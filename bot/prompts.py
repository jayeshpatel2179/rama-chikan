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

# Appended to NEGATIVE_PROMPT only for back-view poses (10/11) — added after
# a real regression where the model took the FRONT's yoke/motif treatment
# and applied it to the back instead of reproducing the actual back
# reference. See BACK_VIEW_FIDELITY below, which is the paired positive-side
# instruction for the same fix.
BACK_VIEW_NEGATIVE_ADDITIONS = (
    "invented yoke, added seam line, mirrored front embroidery, added "
    "centre-back motifs, added sleeve motifs, embellished back"
)

# Inserted into the prompt only for back-view poses. {back_reference_description}
# is filled with a literal, per-product vision description of the actual back
# reference photo (bot.ai.describe_back_reference) — the image reference alone
# was not enough to stop the model inventing back detail that isn't there, so
# the constraint is also stated in words, grounded in what that photo actually shows.
BACK_VIEW_FIDELITY = """\
BACK VIEW FIDELITY — this overrides any general assumption about what a \
chikankari kurti back "usually" looks like: reproduce the back of the \
garment EXACTLY as it appears in the back reference photo, and nowhere \
else. Do not add a yoke panel, seam line, motif column, or any embroidery \
that is not visible in the back reference photo. If the back reference \
shows a plain or lightly embroidered back, render it plain or lightly \
embroidered — plain is the correct answer there, not a mistake to \
"improve" on. Do not carry over the front garment's motif density, motif \
placement, or neckline/yoke treatment onto the back. Copy only what the \
back reference photo actually shows.

FACTUAL DESCRIPTION OF THE BACK REFERENCE PHOTO (verified separately from \
the image itself — treat this as ground truth): {back_reference_description}"""

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
        "head to feet. Shows the back of the garment exactly as the back "
        "reference photo shows it — including back hem, back of the "
        "bottom, and heels of the juttis — with no yoke panel, seam line, "
        "or embroidery added beyond what the back reference actually shows.",
        "full_length",
        requires_back_reference=True,
    ),
    11: Pose(
        11, "Back Three-Quarter, Over-the-Shoulder",
        "Cropped from above the head to roughly hip level. Model's back "
        "is toward the camera, body angled slightly, head turned in soft "
        "profile looking down over the shoulder with a faint smile. Shows "
        "whatever embroidery motif and sleeve detail the back reference "
        "photo actually has — which may be minimal or plain, do not "
        "invent embroidery beyond what it shows — with a warmer, more "
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

    Returns (pose ids to generate, [(blocked_pose_id, reason), ...]).

    Specific mode (owner named exact pose numbers, e.g. "1, 5, 10", or "all
    poses" for all 11): every named pose is generated UNLESS it is a
    genuine physical impossibility — poses 4/6 need a bottom garment that
    doesn't exist on a kurti-only listing. Never soft-skipped here for "no
    back reference": the owner named the pose on purpose, so we trust they
    supplied what it needs. Anything genuinely blocked is returned, not
    silently dropped — the caller (bot/handlers/new_product.py) surfaces it
    to the owner and asks whether to proceed without it, rather than ever
    silently excluding a pose the owner explicitly asked for.

    Count mode (owner just gave a number, bot auto-picks from
    DEFAULT_PRIORITY_ORDER): stays conservative, since the bot is choosing
    blind here — both the bottom and back-reference gates apply, and an
    ineligible pose is simply skipped in favour of the next one in priority
    order without bothering the owner (nothing was explicitly asked for, so
    there's nothing to confirm).
    """

    def hard_block_reason(pose_id: int) -> str | None:
        pose = POSES[pose_id]
        if pose.requires_bottom and listing_type != "kurti_pyjama_set":
            return "listing is kurti-only, no bottom/pyjama exists to show"
        return None

    def auto_ineligible_reason(pose_id: int) -> str | None:
        reason = hard_block_reason(pose_id)
        if reason:
            return reason
        pose = POSES[pose_id]
        if pose.requires_back_reference and not has_back_reference:
            return "no back-side raw reference photo was supplied"
        return None

    if pose_request["mode"] == "specific":
        candidates = [p for p in pose_request["pose_numbers"] if p in POSES]
        selected: list[int] = []
        blocked: list[tuple[int, str]] = []
        for pose_id in candidates:
            reason = hard_block_reason(pose_id)
            if reason:
                blocked.append((pose_id, reason))
            else:
                selected.append(pose_id)
        return selected, blocked

    count = max(1, pose_request["count"])
    selected = []
    for pose_id in DEFAULT_PRIORITY_ORDER:
        if len(selected) >= count:
            break
        if auto_ineligible_reason(pose_id):
            continue
        selected.append(pose_id)
    return selected, []


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


# --- Model age, mapped from the Question 5 category answer ------------------
# The ONLY thing category changes about generation — poses, backgrounds,
# micro-variation, modesty/garment-accuracy rules, output spec, and the
# within-product identity lock (face/hair-style/jewelry/footwear) all stay
# exactly as already specified. This is one variable (MODEL_AGE) swapped
# into the same prompt template.

MODEL_AGE_DESCRIPTIONS = {
    "me": (
        "Adult Indian woman aged 24-30, youthful and contemporary, "
        "medium-fair complexion, natural minimal makeup."
    ),
    "mom": (
        "Adult Indian woman aged 35-45, mature, elegant, and poised, "
        "medium-fair complexion, natural minimal makeup."
    ),
    "me_mom": (
        "Adult Indian woman aged 28-32 — an age that reads plausibly for "
        "both a young adult and a mother, medium-fair complexion, natural "
        "minimal makeup."
    ),
    "all_three": (
        "Adult Indian woman aged 30-35, medium-fair complexion, natural "
        "minimal makeup."
    ),
    "nani": (
        "Adult Indian woman aged 50-60, with a realistic mature build "
        "typical of an average Indian woman that age — not a young "
        "woman's body. Hair is mostly dark with natural salt-and-pepper "
        "greying concentrated at the roots and temples (NOT fully silver "
        "or fully white/grey — greying should read as partial and "
        "natural), styled in a neat low bun or pulled back. Skin shows "
        "subtle, natural signs of age — soft fine lines around the eyes "
        "and mouth, a slightly softer jawline — present but gentle, not "
        "heavily wrinkled or aged beyond her years. Warm gentle "
        "expression, traditional understated jewellery. She must read as "
        "a real, respected Indian grandmother in her 50s or 60s — not a "
        "young model with grey hair painted on, and not an exaggeratedly "
        "elderly caricature either. Tone is warm and respectful: "
        "dignified, elegant, well-presented — never frail, never comedic, "
        "never a caricature."
    ),
}


def resolve_model_age_bucket(categories: list[str]) -> str:
    """Question 5's category answer -> MODEL_AGE bucket.

    "All three" is a deliberate special case that overrides the general
    "Nani wins" rule below it — per the owner's spec, "For Nani" alongside
    exactly one other category still generates the Nani version (that's the
    audience most needing representation), but naming all three together
    is its own distinct blended age instead.
    """
    cats = {c.strip().lower() for c in categories}
    if {"for nani", "for mom", "for me"} <= cats:
        return "all_three"
    if "for nani" in cats:
        return "nani"
    if "for mom" in cats and "for me" in cats:
        return "me_mom"
    if "for mom" in cats:
        return "mom"
    return "me"


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
{back_fidelity_block}
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
    model_age: str,
    back_reference_description: str | None = None,
) -> str:
    pose = POSES[pose_id]

    negative_prompt = NEGATIVE_PROMPT
    back_fidelity_block = ""
    if pose.requires_back_reference:
        negative_prompt = f"{NEGATIVE_PROMPT}, {BACK_VIEW_NEGATIVE_ADDITIONS}"
        back_fidelity_block = "\n" + BACK_VIEW_FIDELITY.format(
            back_reference_description=back_reference_description
            or "(no separate description available — rely on the back "
            "reference image alone, and still follow every rule above.)"
        ) + "\n"

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
        f"{MODEL_AGE_DESCRIPTIONS[model_age]} Hair styled "
        f"{product_identity['hair']}, wearing {product_identity['earrings']} and "
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
        back_fidelity_block=back_fidelity_block,
        negative_prompt=negative_prompt,
        output_spec=OUTPUT_SPEC,
    )
