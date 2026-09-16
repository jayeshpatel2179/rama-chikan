"""Instagram publishing via Upload-Post (Part 5, 2026-09-16), modeled
directly on the same integration already live in the sibling
ped-instacap-poster / pedtalks-insta-image bots — same SDK, same error
shape, same "dummy profile first, live profile is a config change" pattern.

Only the FIRST generated image of a product is ever posted — upload-post
doesn't support carousels here, and Part 5's caption is written for that one
image only (bot.ai.generate_instagram_caption).
"""

import asyncio
import tempfile
from pathlib import Path

from upload_post import UploadPostClient
from upload_post import UploadPostError as _SDKUploadPostError

from bot.config import INSTAGRAM_PROFILE_NAME, UPLOAD_POST_API_KEY


class UploadPostError(Exception):
    """Raised when publishing to Instagram via Upload-Post fails."""


def _upload_sync(image_path: Path, caption: str) -> dict:
    if not UPLOAD_POST_API_KEY or not INSTAGRAM_PROFILE_NAME:
        raise UploadPostError(
            "Instagram posting isn't configured yet — missing "
            "UPLOAD_POST_API_KEY / INSTAGRAM_PROFILE_NAME in .env."
        )

    client = UploadPostClient(api_key=UPLOAD_POST_API_KEY)
    return client.upload_photos(
        [str(image_path)],
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


async def post_first_image(image_bytes: bytes, caption: str) -> str | None:
    """Publish `image_bytes` (the product's FIRST generated image) to the
    configured Instagram profile with `caption`. Returns the published post
    URL, or None if Upload-Post only confirmed the post asynchronously (see
    _extract_instagram_url). Raises UploadPostError on any failure — never
    fails silently, and the caller must not mark the draft as posted unless
    this returns/raises cleanly."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = Path(tmp.name)

    try:
        response = await asyncio.to_thread(_upload_sync, tmp_path, caption)
    except UploadPostError:
        raise
    except _SDKUploadPostError as exc:
        raise UploadPostError(str(exc)) from exc
    except Exception as exc:
        raise UploadPostError(f"Upload-Post request failed: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    return _extract_instagram_url(response)
