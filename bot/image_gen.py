"""On-model product photo generation via GPT Image 2.

Generates one image per requested pose (bot/prompts.py POSES, the 11-pose
library) on the SAME model. Consistency within one product is achieved by:
  - a fixed hair/jewelry/footwear identity chosen once per product
    (bot.prompts.pick_product_identity) and reused in every image's prompt
  - the first generated image is then also passed back in as an explicit
    face/model reference for every subsequent image, so the face itself
    never drifts either
  - one locked background preset per product (bot.prompts.pick_background_preset)

IMAGE_GENERATION_CAP (bot/config.py) currently limits actual generation to
the first N poses in the resolved selection — see the comment there. All
prompt text (safety rules, pose descriptions, listing-type handling,
micro-variation, background) lives in bot/prompts.py; this module is just
the generation loop.
"""

import base64
import io

from openai import AsyncOpenAI
from PIL import Image

from bot import prompts
from bot.config import (
    FINAL_IMAGE_HEIGHT,
    FINAL_IMAGE_WIDTH,
    IMAGE_GENERATION_CAP,
    IMAGE_GEN_MODEL,
    IMAGE_GEN_QUALITY,
    IMAGE_GEN_SIZE,
    OPENAI_API_KEY,
)

_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


def _crop_to_exact_size(png_bytes: bytes) -> bytes:
    """GPT Image 2 only accepts sizes on a 16px grid, which can't land on
    exactly 1000x1250. Center-crop + resize to the exact required output
    size so every product image is pixel-identical in dimensions."""
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    target_ratio = FINAL_IMAGE_WIDTH / FINAL_IMAGE_HEIGHT
    width, height = image.size
    current_ratio = width / height

    if current_ratio > target_ratio:
        new_width = int(height * target_ratio)
        left = (width - new_width) // 2
        image = image.crop((left, 0, left + new_width, height))
    elif current_ratio < target_ratio:
        new_height = int(width / target_ratio)
        top = (height - new_height) // 2
        image = image.crop((0, top, width, top + new_height))

    image = image.resize((FINAL_IMAGE_WIDTH, FINAL_IMAGE_HEIGHT), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


async def generate_model_images(
    raw_photo_bytes: list[bytes],
    color: str,
    material: str,
    kurti_length: str,
    listing_type: str,
    resolved_poses: list[int],
    categories: list[str],
) -> tuple[list[bytes], list[int], list[int]]:
    """resolved_poses: the ordered pose ids already resolved by
    bot.prompts.resolve_pose_selection (Question 9 answer + eligibility
    filtering already applied by the caller).

    categories: Question 5's answer — resolved ONCE per product into a
    MODEL_AGE bucket (bot.prompts.resolve_model_age_bucket) and reused in
    every image's prompt, same as background_preset/product_identity below.

    Returns (images, pose_ids_generated, pose_ids_queued) — queued is
    whatever's left in resolved_poses past IMAGE_GENERATION_CAP, reported
    to the owner rather than silently dropped.
    """
    to_generate = resolved_poses[:IMAGE_GENERATION_CAP]
    queued = resolved_poses[IMAGE_GENERATION_CAP:]

    background_preset = prompts.pick_background_preset()
    product_identity = prompts.pick_product_identity()
    model_age = prompts.resolve_model_age_bucket(categories)
    used_gesture_combos: set = set()

    results: list[bytes] = []
    face_reference: bytes | None = None

    for pose_id in to_generate:
        variation = prompts.pick_variation(used_gesture_combos)

        reference_images = [io.BytesIO(b) for b in raw_photo_bytes]
        for buf in reference_images:
            buf.name = "raw.png"

        if face_reference is not None:
            face_buf = io.BytesIO(face_reference)
            face_buf.name = "face_reference.png"
            reference_images.append(face_buf)

        prompt = prompts.build_pose_prompt(
            pose_id=pose_id,
            color=color,
            material=material,
            kurti_length=kurti_length,
            listing_type=listing_type,
            background_preset=background_preset,
            product_identity=product_identity,
            variation=variation,
            has_face_reference=face_reference is not None,
            model_age=model_age,
        )

        response = await _client.images.edit(
            model=IMAGE_GEN_MODEL,
            image=reference_images,
            prompt=prompt,
            size=IMAGE_GEN_SIZE,
            quality=IMAGE_GEN_QUALITY,
        )
        raw_png = base64.b64decode(response.data[0].b64_json)
        final_png = _crop_to_exact_size(raw_png)
        results.append(final_png)

        if face_reference is None:
            face_reference = raw_png

    return results, to_generate, queued
