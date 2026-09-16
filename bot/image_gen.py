"""On-model product photo generation via GPT Image 2.

Generates one image per requested pose (bot/prompts.py POSES, the 11-pose
standard library, and PREMIUM_POSES, the 11-pose premium library) on the
SAME model. Consistency within one product is achieved by:
  - a fixed hair/jewelry/footwear identity chosen once per product
    (bot.prompts.pick_product_identity) and reused in every image's prompt
  - the first FRONT-facing generated image is then also passed back in as
    an explicit face/model reference for every subsequent front-facing
    pose, so the face itself never drifts either
  - one locked background preset per product (bot.prompts.pick_background_preset)

Front vs. back vs. pyjama garment reference is kept strictly separate, per
bot.prompts.POSE_REFERENCE_BINDING — the single hard source of truth for
which ONE raw photo a given pose may use. This module reads that table
directly rather than re-deriving eligibility per-branch, and raises
MissingReferenceError (never silently substitutes a different reference)
when the bound photo wasn't supplied. This is the fix for a real regression
where a back-facing pose ended up copying the front's embroidery/yoke
treatment — see bot/prompts.py's BACK_VIEW_FIDELITY and
bot.ai.describe_back_reference for the rest of that fix.

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


class MissingReferenceError(Exception):
    """Raised when a pose's bound reference photo (bot.prompts.
    POSE_REFERENCE_BINDING) wasn't supplied. Per the front/back binding fix
    (2026-09-16), a missing reference must STOP generation and ask the shop
    owner for the photo — never silently fall back to a different/pooled
    reference image."""

    def __init__(self, pose_id: int | str, reference_kind: str):
        self.pose_id = pose_id
        self.reference_kind = reference_kind
        super().__init__(
            f"Pose {pose_id} needs the {reference_kind.upper()} reference "
            "photo, which wasn't supplied."
        )


def _resolve_garment_reference(
    pose_id: int | str,
    front_photo: bytes | None,
    back_photo: bytes | None,
    pyjama_photo: bytes | None,
    raw_photo_bytes: list[bytes],
) -> list[bytes]:
    """The ONE reference image list a pose's garment call may use, per
    POSE_REFERENCE_BINDING. Never blends front/back — a back-bound pose
    with no back_photo raises rather than substituting the front photo or
    the undifferentiated raw pool."""
    binding = prompts.POSE_REFERENCE_BINDING[pose_id]

    if binding == prompts.REFERENCE_BACK:
        if back_photo is None:
            raise MissingReferenceError(pose_id, "back")
        return [back_photo]

    if binding == prompts.REFERENCE_PYJAMA:
        # Real pyjama photo is the ground truth when supplied; otherwise
        # the front photo (which shows the full set together for a
        # kurti_pyjama_set listing) is the correct fallback — never the
        # undifferentiated pool, and never the back photo.
        if pyjama_photo is not None:
            return [pyjama_photo]
        if front_photo is not None:
            return [front_photo]
        return raw_photo_bytes

    # REFERENCE_FRONT
    return [front_photo] if front_photo is not None else raw_photo_bytes


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
    pyjama_photo: bytes | None,
    color: str,
    material: str,
    kurti_length: str,
    listing_type: str,
    resolved_poses: list,
    categories: list[str],
) -> tuple[list[bytes], list, list]:
    """resolved_poses: the ordered pose ids already resolved by
    bot.prompts.resolve_pose_selection (Question 9, standard poses 1-11) and
    bot.prompts.resolve_premium_pose_selection (Question 10, premium poses
    "P1".."P8") concatenated by the caller — a mixed list of int and str
    ids. Each is dispatched to its own branch below; nothing about the
    existing int/standard-pose path changes.

    pyjama_photo: set only when the pyjama upload flow (Part 2, 2026-09)
    collected a real PYJAMA_REFERENCE photo for this product. Attached as
    an extra reference image on every front-facing generation call (standard
    and premium alike) whenever listing_type is "kurti_pyjama_set", so the
    generated pyjama matches the real garment instead of being guessed.
    Never attached to back-view poses (10/11), which stay strictly isolated
    to back_photo only — same rule as before this change.

    front_photo / back_photo: set by bot/handlers/new_product.py only when
    exactly 2 raw photos were sent (the normal case — front then back).
    When set, these are the ONLY garment reference used for front-facing vs
    back-facing poses respectively, never blended together — this is the
    fix for a real regression where back views were generated from an
    undifferentiated pool of raw photos (plus a front-posed face reference
    image) and ended up copying the front's yoke/motif treatment onto the
    back. When either is None (1 raw photo, or more than 2 with no reliable
    way to tell which is which), falls back to the old undifferentiated
    raw_photo_bytes pool for that side, same as before this fix.

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

    # Premium-pose state (Question 10) — only actually initialized the
    # first time a premium pose is hit in the loop below, so a product with
    # zero premium poses never consumes a premium-background rotation slot
    # or an unused random pick.
    premium_background_set: str | None = None
    premium_model_variation: dict | None = None
    premium_face_reference: bytes | None = None

    # Resolved once per product (not per image) and reused for every back
    # pose, standard OR premium alike — this is the literal, ground-truth
    # text description injected into the back-view prompt alongside the
    # image (bot/prompts.py's BACK_VIEW_FIDELITY). Only fetched if a back
    # photo actually exists and at least one back-facing pose (standard
    # 10/11 or premium P9/P10) was actually requested.
    def _pose_needs_back_reference(pid: int | str) -> bool:
        if isinstance(pid, str):
            return prompts.PREMIUM_POSES[pid].requires_back_reference
        return prompts.POSES[pid].requires_back_reference

    back_reference_description: str | None = None
    if back_photo is not None and any(_pose_needs_back_reference(p) for p in to_generate):
        back_reference_description = await ai.describe_back_reference(back_photo)

    has_pyjama_reference = listing_type == "kurti_pyjama_set" and pyjama_photo is not None

    results: list[bytes] = []
    face_reference: bytes | None = None
    used_premium_gesture_combos: set = set()

    for pose_id in to_generate:
        binding = prompts.POSE_REFERENCE_BINDING[pose_id]
        garment_bytes = _resolve_garment_reference(
            pose_id, front_photo, back_photo, pyjama_photo, raw_photo_bytes
        )
        reference_images = [io.BytesIO(b) for b in garment_bytes]
        for buf in reference_images:
            buf.name = "raw.png"

        if isinstance(pose_id, str):
            # --- Premium editorial pose (Question 10) ---------------------
            if premium_background_set is None:
                premium_background_set = prompts.pick_premium_background_set()
            if premium_model_variation is None:
                premium_model_variation = prompts.pick_premium_model_variation()
            variation = prompts.pick_premium_variation(used_premium_gesture_combos)

            # PYJAMA_REFERENCE is only ever additive on top of a FRONT-bound
            # pose (P1-P8/P11) — never attached to a back-bound pose (P9/P10).
            pose_has_pyjama_reference = has_pyjama_reference and binding == prompts.REFERENCE_FRONT
            if pose_has_pyjama_reference:
                pyjama_buf = io.BytesIO(pyjama_photo)
                pyjama_buf.name = "pyjama_reference.png"
                reference_images.append(pyjama_buf)

            # Never attach the premium face reference to a back-bound pose —
            # same bleed-through reasoning as the standard branch below.
            use_face_reference = (
                premium_face_reference is not None and binding != prompts.REFERENCE_BACK
            )
            if use_face_reference:
                face_buf = io.BytesIO(premium_face_reference)
                face_buf.name = "face_reference.png"
                reference_images.append(face_buf)

            prompt = prompts.build_premium_pose_prompt(
                pose_id=pose_id,
                color=color,
                material=material,
                kurti_length=kurti_length,
                listing_type=listing_type,
                background_set=premium_background_set,
                model_variation=premium_model_variation,
                variation=variation,
                has_face_reference=use_face_reference,
                has_pyjama_reference=pose_has_pyjama_reference,
                back_reference_description=back_reference_description,
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

            # Only ever seed from a front-bound pose's output — a back-bound
            # premium output shows no face, so it would be a useless (or
            # actively confusing) reference for later premium poses.
            if premium_face_reference is None and binding != prompts.REFERENCE_BACK:
                premium_face_reference = raw_png
            continue

        # --- Standard pose (Question 9) --------------------------------
        variation = prompts.pick_variation(used_gesture_combos)

        # PYJAMA_REFERENCE is only additive on top of a FRONT-bound pose —
        # poses 4/6 are themselves PYJAMA-bound (see _resolve_garment_reference)
        # so they never get it attached a second time here.
        pose_has_pyjama_reference = has_pyjama_reference and binding == prompts.REFERENCE_FRONT
        if pose_has_pyjama_reference:
            pyjama_buf = io.BytesIO(pyjama_photo)
            pyjama_buf.name = "pyjama_reference.png"
            reference_images.append(pyjama_buf)

        # The face reference is a generated image of the model wearing the
        # FRONT's full garment rendering — attaching it to a back-pose call
        # let its garment styling bleed into the back view (the actual bug).
        # Skip it for back poses entirely; hair/jewelry/footwear consistency
        # is already locked independently via the literal product_identity
        # text baked into every prompt (bot.prompts.build_pose_prompt).
        use_face_reference = face_reference is not None and binding != prompts.REFERENCE_BACK
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
            has_pyjama_reference=pose_has_pyjama_reference,
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

        # Only ever seed the face reference from a front-facing pose — a
        # back-view output shows no face, so it would be a useless (or
        # actively confusing) reference for later poses.
        if face_reference is None and binding != prompts.REFERENCE_BACK:
            face_reference = raw_png

    return results, to_generate, queued
