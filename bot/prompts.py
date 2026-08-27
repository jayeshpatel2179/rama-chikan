"""Master image-generation prompt system ("mega prompt") v2 for on-model
product photos, rebuilt 2026-08-27 around a permanent, numbered 11-pose
library (replacing the earlier 9-pose/7-slot system). The shop owner picks
poses by number at intake time (Question 9) instead of the agent deciding a
fixed slot structure — see resolve_pose_selection() below.

Reference photos for these 11 poses live in references/poses/, named
pose_NN_<label>.png matching the POSES dict below 1:1. They're House of
Chikankari's commercial photography, used only to verify these written
descriptions match real posing/cropping/lighting direction — they are never
passed into the image model as generation references (see bot/image_gen.py).

TEMPORARY DEV-PHASE NOTE: bot/config.py's IMAGE_GENERATION_CAP = 1 means
only the FIRST pose the owner selected (or the top of the default priority
order) actually generates right now, to limit API credit burn while
testing. Question 9 is still asked and the full selection is still stored
and resolved — raising the cap later is a one-line config change, not a
rewrite.
"""

import random
from dataclasses import dataclass

from bot import state

# --- Section E: non-negotiable safety/modesty/accuracy rules ---------------
# Carried forward unchanged from v1 — these exist specifically because of
# two real production hallucinations: "without pyjama" being read as "no
# bottoms", and a sleeved kurti being regenerated sleeveless.

SAFETY_RULES = """\
NON-NEGOTIABLE SAFETY AND MODESTY RULES — these override everything else in this prompt:
1. The model is ALWAYS fully clothed, top and bottom, in every single image without exception.
2. A "kurti only" listing NEVER means the model is generated without bottoms — see the LISTING TYPE instruction below for what that actually means.
3. No bare midriff, no bare legs, no bare shoulders, no visible undergarments, no cleavage, no suggestive posing. Fully modest Indian ethnic-wear catalogue standard.
4. Never convert a sleeved garment into a sleeveless one. Sleeve length, neckline shape, and hem length must match the raw reference image exactly.
5. Never invent, remove, or redesign any part of the garment. Embroidery motifs, motif placement, colour, and fabric must be faithful to the raw photo.
6. Never generate a garment surface the raw images do not show. If there is no back-side reference, do not generate a back view."""

NEGATIVE_PROMPT = (
    "nude, topless, bottomless, lingerie, underwear, swimwear, bare legs, "
    "exposed midriff, sleeveless conversion, cleavage, suggestive pose, "
    "revealing, bare shoulders, strapless top, camisole, tank top, text, "
    "watermark, logo"
)

GARMENT_CLEANUP = (
    "The raw reference photos are unironed, wrinkled, casually laid-out "
    "phone shots — render the garment as freshly pressed, crisp, and "
    "professionally steamed on the model, with natural fabric fall and "
    "drape. Smooth out all creases and wrinkles from the reference. Do not "
    "alter the design, motif placement, embroidery density, or colour while "
    "doing this — only the presentation is corrected."
)

OUTPUT_SPEC = (
    "1000x1250 px, 4:5 portrait orientation, sharp focus on the garment, "
    "realistic studio product photography. No text, no watermark, no logo "
    "anywhere in the image."
)

# --- Section D: background presets ------------------------------------------
# Two hardcoded studio presets, LETTERED (not numbered) per this spec. One
# is locked for the whole product (see pick_background_preset), the other
# is used for the next product — persisted across restarts via bot/state.py.

BACKGROUND_PRESETS = {
    "A": (
        "Warm peach-beige textured plaster wall with soft cloudy tonal "
        "variations, paired with a cream and dusty-pink geometric tiled "
        "floor featuring repeating diamond shapes, subtle circular "
        "patterns, and thin brown floral grid accents."
    ),
    "B": (
        "Warm beige textured plaster wall with subtle aged tonal "
        "variations and simple wooden skirting at the base, paired with a "
        "light natural wood-plank floor and a faded vintage brown-beige "
        "woven carpet along the foreground."
    ),
}

LIGHTING = (
    "Soft diffused studio key light from the front-left, gentle falloff, "
    "soft natural shadow on the wall behind the model, no harsh shadows, "
    "no colour cast, warm neutral white balance, consistent camera height "
    "across every shot — it must read as one continuous photoshoot."
)


def pick_background_preset() -> str:
    """Call ONCE per product; every image for that product reuses the same
    literal preset text. Alternates across products, persisted to survive
    bot restarts (bot/state.py)."""
    return state.next_background_preset()


# --- Section A: the 11-pose library -----------------------------------------


@dataclass(frozen=True)
class Pose:
    id: int
    label: str
    description: str
    crop_type: str  # "full_length" | "waist_up" | "detail" | "bottom_only"
    requires_bottom: bool = False
    requires_back_reference: bool = False


POSES: dict[int, Pose] = {
    1: Pose(
        1, "Full-Length Front, Straight",
        "Model faces camera square-on, full body head to feet in frame. "
        "Feet close together. Full kurti and full bottom visible down to "
        "the juttis. This is the primary hero shot.",
        "full_length",
    ),
    2: Pose(
        2, "Waist-Up Front, Hands Clasped",
        "Cropped from just above the head to roughly mid-thigh. Model "
        "faces front. Shows the yoke embroidery and sleeve detail clearly.",
        "waist_up",
    ),
    3: Pose(
        3, "Embroidery Detail Crop, Face Cropped",
        "Tight crop from roughly the chin/lips line down to hip level. "
        "Face deliberately cut above the mouth so only the jaw, earrings "
        "and neckline are visible. Frame fills with the neckline yoke "
        "embroidery, chikankari motif density, and fabric texture. This "
        "is the fabric-detail shot.",
        "detail",
    ),
    4: Pose(
        4, "Bottom-Only, Waist to Feet",
        "Framed from the waistband down to the floor. Only the "
        "pyjama/palazzo is the subject. A plain neutral top is visible at "
        "the very top edge of the frame. Face and torso fully out of "
        "frame. Juttis visible at the bottom.",
        "bottom_only",
        requires_bottom=True,
    ),
    5: Pose(
        5, "Full-Length Three-Quarter, Looking Away",
        "Body rotated roughly 30-40 degrees to one side, weight settled, "
        "one foot slightly forward. Full body head to feet, showing the "
        "side seam, side slit and drape.",
        "full_length",
    ),
    6: Pose(
        6, "Hem and Footwear Close-Up",
        "Extreme close-up of the bottom hem and ankles, showing hem "
        "embroidery, fabric fall and the juttis on the floor. Nothing "
        "above mid-calf in frame.",
        "detail",
        requires_bottom=True,
    ),
    7: Pose(
        7, "Full-Length Three-Quarter, Downward Gaze with Smile",
        "Body angled to one side, full length in frame. Relaxed, candid "
        "feel.",
        "full_length",
    ),
    8: Pose(
        8, "Waist-Up Three-Quarter, Head Tilted Down, Eyes Lowered",
        "Cropped from above the head to roughly hip level. Body at a soft "
        "angle. Hair falling forward over one shoulder. Emphasises "
        "neckline embroidery with a soft editorial mood.",
        "waist_up",
    ),
    9: Pose(
        9, "Waist-Up Three-Quarter, Looking to the Side",
        "Cropped from above the head to hip level. Body angled. Profile "
        "of the jhumka earring visible.",
        "waist_up",
    ),
    10: Pose(
        10, "Full-Length Back View, Straight",
        "Model faces fully away from camera, standing straight, full body "
        "head to feet. Shows back yoke embroidery, back hem, back of the "
        "bottom, and heels of the juttis.",
        "full_length",
        requires_back_reference=True,
    ),
    11: Pose(
        11, "Back Three-Quarter, Over-the-Shoulder",
        "Cropped from above the head to roughly hip level. Model's back "
        "is toward the camera, body angled slightly, head turned in soft "
        "profile looking down over the shoulder with a faint smile. Shows "
        "back embroidery motif and sleeve detail with a warmer, more "
        "human feel than pose 10.",
        "waist_up",
        requires_back_reference=True,
    ),
}

# Used when the owner gives only a count (e.g. "5") instead of specific
# numbers. Guarantees the hero shot, a side angle, and a detail shot come
# first.
DEFAULT_PRIORITY_ORDER = [1, 5, 2, 3, 10, 7, 4, 9, 11, 8, 6]


def resolve_pose_selection(
    pose_request: dict,
    listing_type: str,
    has_back_reference: bool,
) -> tuple[list[int], list[tuple[int, str]]]:
    """pose_request: {"mode": "specific"|"count", "pose_numbers": [...], "count": N}

    Returns (ordered pose ids to actually generate, [(skipped_pose_id, reason), ...]).
    Bottom-dependent poses (4, 6) are dropped for kurti-only listings;
    back-dependent poses (10, 11) are dropped when no back-side raw photo
    was supplied — never invented, per the safety rules above.
    """

    def ineligible_reason(pose_id: int) -> str | None:
        pose = POSES[pose_id]
        if pose.requires_bottom and listing_type != "kurti_pyjama_set":
            return "listing is kurti-only, no bottom to show"
        if pose.requires_back_reference and not has_back_reference:
            return "no back-side raw reference photo was supplied"
        return None

    selected: list[int] = []
    skipped: list[tuple[int, str]] = []

    if pose_request["mode"] == "specific":
        candidates = [p for p in pose_request["pose_numbers"] if p in POSES]
        for pose_id in candidates:
            reason = ineligible_reason(pose_id)
            if reason:
                skipped.append((pose_id, reason))
            else:
                selected.append(pose_id)
    else:
        count = max(1, pose_request["count"])
        for pose_id in DEFAULT_PRIORITY_ORDER:
            if len(selected) >= count:
                break
            reason = ineligible_reason(pose_id)
            if reason:
                continue  # not a real "skip" — just not chosen for auto-select
            selected.append(pose_id)

    return selected, skipped


# --- Section C: micro-variation library -------------------------------------
# Hand/head/eye/expression/stance vary PER IMAGE (with a no-repeat
# hand+head+eye combo rule within one product). Hair/jewelry/footwear are
# chosen ONCE PER PRODUCT and stay identical across every image of that
# product — variation happens between products, not within one.

HAND_POSITIONS = [
    "arms straight at the sides",
    "one hand lightly touching the kurta neckline",
    "one hand resting at the hip",
    "both hands loosely clasped at the waist",
    "one hand adjusting a bangle",
    "one arm slightly bent with fingers relaxed",
]
HEAD_DIRECTIONS = [
    "facing forward",
    "turned slightly left",
    "turned slightly right",
    "tilted gently down",
    "lifted slightly up",
]
EYE_DIRECTIONS = [
    "direct at the camera",
    "looking off to the left",
    "looking off to the right",
    "cast downward",
    "eyes softly lowered",
]
EXPRESSIONS = ["neutral composed", "a faint closed-lip smile", "a soft warm smile"]
STANCES = [
    "feet together",
    "one foot slightly forward",
    "weight shifted onto one hip",
    "mid-stride",
]

HAIR_STYLES = [
    "loose, falling over one shoulder",
    "loose, down the back",
    "in a low sleek bun",
]
# (earrings, bracelet/bangle) pairs — kept paired so the two always make sense together.
JEWELRY_STYLES = [
    ("silver jhumka earrings", "a single bangle"),
    ("gold jhumka earrings", "a stacked bangle set"),
    ("small stud earrings", "a plain metal kada"),
]
FOOTWEAR_STYLES = [
    "silver sequin juttis",
    "ivory embroidered juttis",
    "natural tan leather juttis",
    "gold zari juttis",
]


def pick_product_identity() -> dict:
    """Call ONCE per product. Fixed across every image of that product."""
    earrings, bracelet = random.choice(JEWELRY_STYLES)
    return {
        "hair": random.choice(HAIR_STYLES),
        "earrings": earrings,
        "bracelet": bracelet,
        "footwear": random.choice(FOOTWEAR_STYLES),
    }


def pick_variation(used_combos: set) -> dict:
    """Call once per image within a product. `used_combos` is a
    set[tuple[str, str, str]] of (hand, head, eye) already used for this
    product — mutated in place to track the new pick."""
    for _ in range(50):
        hand = random.choice(HAND_POSITIONS)
        head = random.choice(HEAD_DIRECTIONS)
        eye = random.choice(EYE_DIRECTIONS)
        combo = (hand, head, eye)
        if combo not in used_combos:
            used_combos.add(combo)
            break
    else:
        # Exhausted realistic combos (only possible with >~10 images on one
        # product) — reuse rather than loop forever.
        used_combos.add(combo)
    return {
        "hand": hand,
        "head": head,
        "eye": eye,
        "expression": random.choice(EXPRESSIONS),
        "stance": random.choice(STANCES),
    }


# --- Section B: listing-type rule (the fix for the "without pyjama"
# hallucination, carried forward from v1) ------------------------------------


def _indefinite_article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _listing_rule(listing_type: str, color: str) -> str:
    if listing_type == "kurti_pyjama_set":
        article = _indefinite_article(color)
        return (
            f"This listing is {article} {color} kurti with its matching "
            "pyjama, salwar, or palazzo, exactly as shown together in the "
            "raw reference photos. The model wears the full set."
        )
    return (
        f"This listing is the {color} kurti ONLY — the bottom is NOT part "
        "of what is being sold. The model still wears a bottom at all "
        "times: a plain, unembellished, neutral solid churidar or straight "
        "palazzo in a colour that quietly coordinates with the kurti "
        "(ivory, off-white, or a tonal match) — never patterned, never the "
        "raw reference's actual bottom, and never absent. This bottom is "
        "styled as a plain base layer only, never the visual focus. "
        "Framing may crop at mid-calf or mid-thigh to emphasise the kurti, "
        "but the bottom must always be visibly present at the crop line — "
        "never crop above the bottom entirely, and never show bare legs."
    )


# --- The mega prompt template ------------------------------------------------

MEGA_PROMPT_TEMPLATE = """\
{safety_rules}

MODEL: {model_identity}{face_clause}

GARMENT: Photorealistic e-commerce fashion photo of the model wearing the \
exact {color} kurti shown in the raw reference photos, made of {material}, \
with the same chikankari embroidery detail, fabric texture, and {color} \
colour faithfully reproduced. {length_rule}

LISTING TYPE: {listing_rule}

POSE {pose_id} — {pose_label}: {pose_description}

GESTURE FOR THIS IMAGE: {hand}; head {head}; eyes {eye}; expression: \
{expression}; stance: {stance}.

BACKGROUND (locked for this entire product, identical in every image): \
{background}

LIGHTING: {lighting}

GARMENT PRESENTATION: {cleanup_rule}

FIDELITY: Do not invent, guess, or mirror any part of the garment that is \
not visible in the reference photos — render only what is actually shown \
there.

DO NOT INCLUDE ANY OF: {negative_prompt}

OUTPUT: {output_spec}
"""


def build_pose_prompt(
    *,
    pose_id: int,
    color: str,
    material: str,
    kurti_length: str,
    listing_type: str,
    background_preset: str,
    product_identity: dict,
    variation: dict,
    has_face_reference: bool,
) -> str:
    pose = POSES[pose_id]

    length_rule = (
        "The kurti is SHORT length (hip to mid-thigh) — render the "
        "proportions as a short kurti, not a long one."
        if kurti_length == "short"
        else "The kurti is LONG length (knee-length or longer) — render "
        "the full correct length."
    )

    face_clause = (
        " The model's face, skin tone, hair, jewelry, and footwear must "
        "exactly match the model shown in the additional reference image "
        "— this is the same person, in the same accessories, just a "
        "different pose."
        if has_face_reference
        else ""
    )

    model_identity = (
        "Adult Indian woman in her mid-20s, medium-fair complexion, "
        f"natural minimal makeup, hair styled {product_identity['hair']}, "
        f"wearing {product_identity['earrings']} and "
        f"{product_identity['bracelet']}, and {product_identity['footwear']}. "
        "Calm, composed, professional catalogue expression as a baseline "
        "(see gesture below for this specific image)."
    )

    return MEGA_PROMPT_TEMPLATE.format(
        safety_rules=SAFETY_RULES,
        model_identity=model_identity,
        face_clause=face_clause,
        color=color,
        material=material,
        length_rule=length_rule,
        listing_rule=_listing_rule(listing_type, color),
        pose_id=pose.id,
        pose_label=pose.label,
        pose_description=pose.description,
        hand=variation["hand"],
        head=variation["head"],
        eye=variation["eye"],
        expression=variation["expression"],
        stance=variation["stance"],
        background=BACKGROUND_PRESETS[background_preset],
        lighting=LIGHTING,
        cleanup_rule=GARMENT_CLEANUP,
        negative_prompt=NEGATIVE_PROMPT,
        output_spec=OUTPUT_SPEC,
    )
