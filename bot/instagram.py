"""Instagram + Facebook publishing via Upload-Post (Part 5, 2026-09-16;
Facebook added 2026-09-23; real-status polling fixed 2026-09-23), modeled
directly on the same integration already live in the sibling
ped-instacap-poster / pedtalks-insta-image bots — same SDK, same error
shape, same "dummy profile first, live profile is a config change" pattern.

All generated images of a product are posted together as one carousel per
platform (Upload-Post's photos[] array, max 10 items), with one caption
that bot.ai.generate_instagram_caption writes from the FIRST image, sent to
BOTH platforms via a single platform[] = ["instagram", "facebook"] call —
not two separate requests. (An earlier version posted only the first image
to Instagram alone, on the mistaken assumption that Upload-Post had no
carousel support.)

Facebook posting requires the Rama Chikan Facebook Page to be connected to
the same Upload-Post profile as Instagram (INSTAGRAM_PROFILE_NAME) — that's
a manual step in the Upload-Post dashboard, not something this module can
do.

IMPORTANT, found via a real failed test (2026-09-23): Upload-Post frequently
hands a multi-platform photo request off to a BACKGROUND worker instead of
resolving both platforms synchronously — the initial response is just
{"success": true, "request_id"/"job_id": ...}, with no per-platform result
yet. An earlier version of this module treated that "accepted" response as
"every requested platform succeeded", which is wrong: in the real failure
that prompted this fix, Instagram genuinely posted but Facebook silently
never completed (the profile being tested had no Facebook account
connected), and the bot told the shop owner both had posted. This module
now polls Upload-Post's own /uploadposts/status endpoint (get_status) for
the real per-platform outcome before reporting anything as posted — see
_poll_async_status and _extract_platform_results below.
"""

import asyncio
import tempfile
import time
from pathlib import Path

from upload_post import UploadPostClient
from upload_post import UploadPostError as _SDKUploadPostError

from bot.config import INSTAGRAM_PROFILE_NAME, UPLOAD_POST_API_KEY

MAX_CAROUSEL_ITEMS = 10  # Instagram's own limit per carousel post
PLATFORMS = ["instagram", "facebook"]

# How long we're willing to poll Upload-Post's background worker for a real
# per-platform result before giving up and reporting "still processing" for
# whatever hasn't resolved yet. Chosen to comfortably cover normal
# processing time without leaving the shop owner staring at "Posting..." in
# Telegram for too long; a platform that's still unresolved after this is
# reported as pending, never silently assumed successful.
_ASYNC_POLL_INTERVAL_SECONDS = 3
_ASYNC_POLL_MAX_ATTEMPTS = 10  # ~30s total


class UploadPostError(Exception):
    """Raised when publishing via Upload-Post fails at the request level
    (bad credentials, network error, unparseable response) — not for a
    single platform's own failure, which is reported per-platform instead
    (see PlatformResult)."""


class PlatformResult:
    """One platform's outcome. Three real states, never collapsed into a
    plain boolean:
      - success=True, url set (or None if Upload-Post hasn't handed back a
        URL yet, which happens even on a genuine success)
      - success=False, error set to why it failed
      - success=False, pending=True: we polled for the real result and it
        still hadn't resolved when we stopped waiting — NOT a confirmed
        failure, just unresolved. Never reported to the shop owner as
        "posted"."""

    def __init__(
        self,
        platform: str,
        success: bool,
        url: str | None = None,
        error: str | None = None,
        pending: bool = False,
    ):
        self.platform = platform
        self.success = success
        self.url = url
        self.error = error
        self.pending = pending


def _upload_sync(image_paths: list[Path], caption: str) -> dict:
    if not UPLOAD_POST_API_KEY or not INSTAGRAM_PROFILE_NAME:
        raise UploadPostError(
            "Instagram posting isn't configured yet — missing "
            "UPLOAD_POST_API_KEY / INSTAGRAM_PROFILE_NAME in .env."
        )

    client = UploadPostClient(api_key=UPLOAD_POST_API_KEY)
    return client.upload_photos(
        [str(p) for p in image_paths],
        title=caption,
        user=INSTAGRAM_PROFILE_NAME,
        platforms=PLATFORMS,
        media_type="IMAGE",
    )


def _poll_status_sync(request_id: str) -> dict:
    """Blocking poll of Upload-Post's real status for an async job — run in
    the same worker thread as the upload itself (see post_images). Returns
    the last status response seen, whether or not every platform finished;
    never raises for a timeout, since "still processing" is a legitimate,
    reportable state here, not an error."""
    client = UploadPostClient(api_key=UPLOAD_POST_API_KEY)
    status: dict = {}
    for attempt in range(_ASYNC_POLL_MAX_ATTEMPTS):
        status = client.get_status(request_id=request_id)
        if attempt + 1 < _ASYNC_POLL_MAX_ATTEMPTS and status.get("completed") != status.get("total"):
            time.sleep(_ASYNC_POLL_INTERVAL_SECONDS)
            continue
        break
    return status


def _platform_result_from_entry(platform: str, entry: dict | None) -> PlatformResult:
    if not isinstance(entry, dict):
        return PlatformResult(platform, False, error="No result returned for this platform.")
    if entry.get("success") is False:
        return PlatformResult(platform, False, error=entry.get("error_message") or entry.get("error") or "unknown error")
    if entry.get("success") is True:
        return PlatformResult(platform, True, url=entry.get("post_url") or entry.get("url"))
    return PlatformResult(platform, False, error=f"Unexpected per-platform result: {entry}")


def _extract_platform_results(response: dict) -> dict[str, PlatformResult]:
    """Per-platform results for every entry in PLATFORMS, from one
    Upload-Post response. Handles the synchronous dict-keyed shape
    (`results: {"instagram": {...}, "facebook": {...}}`) directly; for the
    asynchronous "accepted, processing in the background" shape, polls the
    real status (_poll_status_sync) rather than assuming success — see the
    module docstring for why that assumption was wrong in practice."""
    if not isinstance(response, dict):
        raise UploadPostError(f"Unexpected response from Upload-Post: {response}")

    results = response.get("results")
    if isinstance(results, dict):
        return {p: _platform_result_from_entry(p, results.get(p)) for p in PLATFORMS}

    request_id = response.get("request_id") or response.get("job_id")
    if response.get("success") is True and request_id:
        status = _poll_status_sync(request_id)
        by_platform = {r.get("platform"): r for r in status.get("results", []) if isinstance(r, dict)}
        out: dict[str, PlatformResult] = {}
        for platform in PLATFORMS:
            entry = by_platform.get(platform)
            if entry is not None:
                out[platform] = _platform_result_from_entry(platform, entry)
            else:
                out[platform] = PlatformResult(
                    platform, False, pending=True,
                    error="Still processing at Upload-Post — check the dashboard shortly.",
                )
        return out

    raise UploadPostError(f"Unexpected response from Upload-Post: {response}")


async def post_images(images: list[bytes], caption: str) -> dict[str, PlatformResult]:
    """Publish the product's generated images to the configured Instagram
    AND Facebook profile with one `caption`, in a single Upload-Post call
    (platform[] = ["instagram", "facebook"], not two separate requests).
    Two or more images go out as a carousel on each platform (Upload-Post's
    photos[] array); one image is a normal single-photo post. Each platform
    allows at most MAX_CAROUSEL_ITEMS per carousel, so anything past that is
    left out (in order — the first 10).

    Returns a dict keyed by platform (see PLATFORMS) with each platform's
    own PlatformResult (success / failure / still-pending — see that
    class). Instagram can succeed while Facebook fails or is still
    processing, and the caller reports each independently. Raises
    UploadPostError only for a request-level failure (bad credentials,
    network error, unparseable response) that made it impossible to get ANY
    per-platform result."""
    if not images:
        raise UploadPostError("No images to post.")

    tmp_paths: list[Path] = []
    try:
        for image_bytes in images[:MAX_CAROUSEL_ITEMS]:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(image_bytes)
                tmp_paths.append(Path(tmp.name))

        response = await asyncio.to_thread(_upload_sync, tmp_paths, caption)
        return await asyncio.to_thread(_extract_platform_results, response)
    except UploadPostError:
        raise
    except _SDKUploadPostError as exc:
        raise UploadPostError(str(exc)) from exc
    except Exception as exc:
        raise UploadPostError(f"Upload-Post request failed: {exc}") from exc
    finally:
        for path in tmp_paths:
            path.unlink(missing_ok=True)
