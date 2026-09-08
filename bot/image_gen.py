"""On-model product photo generation via GPT Image 2.

Generates one image per requested pose (bot/prompts.py POSES, the 11-pose
library) on the SAME model. Consistency within one product is achieved by:
  - a fixed hair/jewelry/footwear identity chosen once per product
    (bot.prompts.pick_product_identity) and reused in every image's prompt
  - the first FRONT-facing generated image is then also passed back in as
    an explicit face/model reference for every subsequent front-facing
    pose, so the face itself never drifts either
  - one locked background preset per product (bot.prompts.pick_background_preset)

Front vs. back garment reference is kept strictly separate: front-facing
poses use only the front raw photo, back-facing poses (10/11) use only the
back raw photo — never blended, and the front-posed face reference is never
attached to a back-pose call either, since its rendered garment would bleed
the front's embroidery/yoke into the back view. See bot/prompts.py's
BACK_VIEW_FIDELITY and bot.ai.describe_back_reference for the rest of that
fix.

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

from bot import ai, prompts
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
    front_photo: bytes | None,
    back_photo: bytes | None,
    color: str,
    material: str,
    kurti_length: str,
    listing_type: str,
    resolved_poses: list[int],
    categories: list[str],
    embroidery_thread_color: str = "white_or_cream",
    embroidery_thread_color_name: str = "",
    back_style: str = "not_specified",
    neckline_closeup: bytes | None = None,
    back_yoke_closeup: bytes | None = None,
    sleeve_closeup: bytes | None = None,
    fabric_closeup: bytes | None = None,
) -> tuple[list[bytes], list[int], list[int], list[int]]:
    """resolved_poses: the ordered pose ids already resolved by
    bot.prompts.resolve_pose_selection (Question 11 answer + eligibility
    filtering already applied by the caller).

    front_photo / back_photo: set by bot/handlers/new_product.py whenever 2
    or more raw photos were sent (the normal case — front then back, plus
    optional close-ups after). When set, these are the ONLY garment
    reference used for front-facing vs back-facing poses respectively,
    never blended together — this is the fix for a real regression where
    back views were generated from an undifferentiated pool of raw photos
    (plus a front-posed face reference image) and ended up copying the
    front's yoke/motif treatment onto the back. When either is None (only 1
    raw photo, with no reliable way to tell front from back), falls back to
    the old undifferentiated raw_photo_bytes pool for that side, same as
    before this fix.

    neckline_closeup / back_yoke_closeup / sleeve_closeup / fabric_closeup:
    optional extra reference photos (raw photos 3-6, see
    bot/handlers/new_product.py) added after a real hallucination where a
    tonal/low-contrast garment gave the model too little signal from just 2
    flat shots. neckline_closeup only supplements pose 3 (the embroidery
    detail crop); back_yoke_closeup only supplements back-facing poses
    10/11; sleeve_closeup and fabric_closeup supplement every pose, since
    sleeve and fabric detail matter regardless of which side is framed.

    embroidery_thread_color / embroidery_thread_color_name (Question 9) and
    back_style (Question 10): passed straight through to every pose's
    prompt (bot.prompts.build_pose_prompt) — see that module for how they
    become hard constraints rather than hints.

    categories: Question 5's answer — resolved ONCE per product into a
    MODEL_AGE bucket (bot.prompts.resolve_model_age_bucket) and reused in
    every image's prompt, same as background_preset/product_identity below.

    Returns (images, pose_ids_generated, pose_ids_queued, pose_ids_flagged).
    queued is whatever's left in resolved_poses past IMAGE_GENERATION_CAP,
    reported to the owner rather than silently dropped. flagged is any
    back-facing pose that still didn't match its reference after one
    regeneration attempt (see the verify-and-retry block below) — surfaced
    to the owner as a review warning rather than silently shipped.
    """
    to_generate = resolved_poses[:IMAGE_GENERATION_CAP]
    queued = resolved_poses[IMAGE_GENERATION_CAP:]

    background_preset = prompts.pick_background_preset()
    product_identity = prompts.pick_product_identity()
    model_age = prompts.resolve_model_age_bucket(categories)
    used_gesture_combos: set = set()

    # Resolved once per product (not per image) and reused for every back
    # pose — this is the literal, ground-truth text description injected
    # into the back-view prompt alongside the image (bot/prompts.py's
    # BACK_VIEW_FIDELITY). Only fetched if a back photo actually exists and
    # a back-facing pose was actually requested.
    back_reference_description: str | None = None
    if back_photo is not None and any(
        prompts.POSES[p].requires_back_reference for p in to_generate
    ):
        back_reference_description = await ai.describe_back_reference(back_photo)

    results: list[bytes] = []
    flagged: list[int] = []
    face_reference: bytes | None = None

    for pose_id in to_generate:
        pose = prompts.POSES[pose_id]
        variation = prompts.pick_variation(used_gesture_combos)

        if pose.requires_back_reference:
            # BACK_REFERENCE only — never the front photo, never blended
            # with it. See the docstring above for why.
            garment_bytes = [back_photo] if back_photo is not None else raw_photo_bytes
            if back_yoke_closeup is not None:
                garment_bytes = garment_bytes + [back_yoke_closeup]
        else:
            garment_bytes = [front_photo] if front_photo is not None else raw_photo_bytes
            if pose_id == 3 and neckline_closeup is not None:
                garment_bytes = garment_bytes + [neckline_closeup]
        if sleeve_closeup is not None:
            garment_bytes = garment_bytes + [sleeve_closeup]
        if fabric_closeup is not None:
            garment_bytes = garment_bytes + [fabric_closeup]

        reference_images = [io.BytesIO(b) for b in garment_bytes]
        for buf in reference_images:
            buf.name = "raw.png"

        # The face reference is a generated image of the model wearing the
        # FRONT's full garment rendering — attaching it to a back-pose call
        # let its garment styling bleed into the back view (the actual bug).
        # Skip it for back poses entirely; hair/jewelry/footwear consistency
        # is already locked independently via the literal product_identity
        # text baked into every prompt (bot.prompts.build_pose_prompt).
        use_face_reference = face_reference is not None and not pose.requires_back_reference
        if use_face_reference:
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
            has_face_reference=use_face_reference,
            model_age=model_age,
            back_reference_description=back_reference_description,
            embroidery_thread_color=embroidery_thread_color,
            embroidery_thread_color_name=embroidery_thread_color_name,
            back_style=back_style,
        )

        response = await _client.images.edit(
            model=IMAGE_GEN_MODEL,
            image=reference_images,
            prompt=prompt,
            size=IMAGE_GEN_SIZE,
            quality=IMAGE_GEN_QUALITY,
        )
        raw_png = base64.b64decode(response.data[0].b64_json)

        # Verify-and-retry: catches exactly the failure mode that prompted
        # this block — a back view that invents a yoke/medallion or renders
        # tonal embroidery in the wrong colour despite the fidelity rules
        # above. One regeneration attempt with the specific issue appended
        # as a strict correction, then accept whatever comes out (never
        # loops indefinitely) — if it's STILL wrong, it's surfaced to the
        # owner as a review warning (draft["flagged_poses"] in
        # bot/handlers/new_product.py) rather than silently shipped either way.
        if pose.requires_back_reference and back_photo is not None:
            verification = await ai.verify_back_view(
                raw_png, back_photo, back_reference_description or ""
            )
            if not verification["matches_reference"]:
                retry_images = [io.BytesIO(b) for b in garment_bytes]
                for buf in retry_images:
                    buf.name = "raw.png"
                strengthened_prompt = (
                    prompt
                    + "\n\nSTRICT CORRECTION (a previous attempt failed this "
                    "exact check — fix it): " + verification["issues"]
                )
                retry_response = await _client.images.edit(
                    model=IMAGE_GEN_MODEL,
                    image=retry_images,
                    prompt=strengthened_prompt,
                    size=IMAGE_GEN_SIZE,
                    quality=IMAGE_GEN_QUALITY,
                )
                raw_png = base64.b64decode(retry_response.data[0].b64_json)
                recheck = await ai.verify_back_view(
                    raw_png, back_photo, back_reference_description or ""
                )
                if not recheck["matches_reference"]:
                    flagged.append(pose_id)

        final_png = _crop_to_exact_size(raw_png)
        results.append(final_png)

        # Only ever seed the face reference from a front-facing pose — a
        # back-view output shows no face, so it would be a useless (or
        # actively confusing) reference for later poses.
        if face_reference is None and not pose.requires_back_reference:
            face_reference = raw_png

    return results, to_generate, queued, flagged
