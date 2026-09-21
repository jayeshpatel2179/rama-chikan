"""Instagram publishing via Upload-Post (Part 5, 2026-09-16), modeled
directly on the same integration already live in the sibling
ped-instacap-poster / pedtalks-insta-image bots — same SDK, same error
shape, same "dummy profile first, live profile is a config change" pattern.

All generated images of a product are posted together as one Instagram
carousel (Upload-Post's photos[] array, max 10 items), with one caption that
bot.ai.generate_instagram_caption writes from the FIRST image. (An earlier
version posted only the first image on the mistaken assumption that
Upload-Post had no carousel support.)
"""

import asyncio
import tempfile
from pathlib import Path

from upload_post import UploadPostClient
from upload_post import UploadPostError as _SDKUploadPostError

from bot.config import INSTAGRAM_PROFILE_NAME, UPLOAD_POST_API_KEY


MAX_CAROUSEL_ITEMS = 10  # Instagram's own limit per carousel post


class UploadPostError(Exception):
    """Raised when publishing to Instagram via Upload-Post fails."""


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
        platforms=["instagram"],
        media_type="IMAGE",
    )


def _extract_instagram_url(response: dict) -> str | None:
    """Returns the published post URL, or None if Upload-Post accepted the
    post but only confirms it asynchronously (see the "backgrounded" shape
    below — no URL available yet)."""
    if not isinstance(response, dict):
        raise UploadPostError(f"Unexpected response from Upload-Post: {response}")

    results = response.get("results")
    if isinstance(results, dict):
        instagram_result = results.get("instagram")
        if not isinstance(instagram_result, dict):
            raise UploadPostError(f"Unexpected response from Upload-Post: {response}")

        if instagram_result.get("success") is False:
            error = instagram_result.get("error") or "unknown error"
            raise UploadPostError(f"Instagram publish failed: {error}")

        url = instagram_result.get("url")
        if not url:
            raise UploadPostError(f"Upload-Post didn't return a post URL: {response}")

        return url

    # Upload-Post sometimes hands a slow request off to a background worker
    # instead of returning the synchronous {"results": {...}} shape above —
    # it still reports success (per its own "success": true), just without
    # a URL yet.
    if response.get("success") is True and (response.get("request_id") or response.get("job_id")):
        return None

    raise UploadPostError(f"Unexpected response from Upload-Post: {response}")


async def post_images(images: list[bytes], caption: str) -> str | None:
    """Publish the product's generated images to the configured Instagram
    profile with one `caption`. Two or more images go out as a single
    carousel post (Upload-Post's photos[] array); one image is a normal
    single-photo post. Instagram allows at most MAX_CAROUSEL_ITEMS per
    carousel, so anything past that is left out (in order — the first 10).

    Returns the published post URL, or None if Upload-Post only confirmed
    the post asynchronously (see _extract_instagram_url). Raises
    UploadPostError on any failure — never fails silently, and the caller
    must not mark the draft as posted unless this returns cleanly."""
    if not images:
        raise UploadPostError("No images to post.")

    tmp_paths: list[Path] = []
    try:
        for image_bytes in images[:MAX_CAROUSEL_ITEMS]:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(image_bytes)
                tmp_paths.append(Path(tmp.name))

        response = await asyncio.to_thread(_upload_sync, tmp_paths, caption)
    except UploadPostError:
        raise
    except _SDKUploadPostError as exc:
        raise UploadPostError(str(exc)) from exc
    except Exception as exc:
        raise UploadPostError(f"Upload-Post request failed: {exc}") from exc
    finally:
        for path in tmp_paths:
            path.unlink(missing_ok=True)

    return _extract_instagram_url(response)
