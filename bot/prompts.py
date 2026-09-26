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

# Appended to NEGATIVE_PROMPT only for back-view poses (standard 10/11,
# premium P9/P10) — added after a real regression where the model took the
# FRONT's yoke/motif treatment and applied it to the back instead of
# reproducing the actual back reference. See BACK_VIEW_FIDELITY below, which
# is the paired positive-side instruction for the same fix.
BACK_VIEW_NEGATIVE_ADDITIONS = (
    "front yoke embroidery, mirrored front design, invented yoke panel, "
    "added seam line, added centre-back motif cluster, front neckline on "
    "back, added sleeve motifs, embellished back"
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
    crop_type: str  # "full_length" | "waist_up" | "detail" | "bottom_only" | "knee_length"
    requires_bottom: bool = False
    requires_back_reference: bool = False
    # Set only for a pose derived by cropping another pose's ALREADY-GENERATED
    # output (2026-09-26, poses 12/13) rather than generated via its own API
    # call. See CROP_DEPENDENCY / image_gen.py's crop-derived-pose branch.
    crop_source_pose: int | None = None


# --- Reference binding table (Part 2, 2026-09-16 front/back mixup fix) -----
# The single hard source of truth for which ONE raw-photo reference a given
# pose (standard int id or premium "Pxx" id) is allowed to use. Both
# bot/image_gen.py's standard and premium generation branches read this
# table directly to pick reference_images, instead of re-deriving
# eligibility per-branch — this is what closes the gap that let a back pose
# fall through to an undifferentiated photo pool (which could contain the
# front photo) when back_photo happened to be unset. A back-facing pose must
# NEVER receive the front image as its reference, blended or otherwise; this
# table is what image_gen.py enforces that against, with a hard stop (see
# image_gen.MissingReferenceError) rather than a silent substitution when
# the bound reference is missing.
REFERENCE_FRONT = "front"
REFERENCE_BACK = "back"
REFERENCE_PYJAMA = "pyjama"

POSE_REFERENCE_BINDING: dict[int | str, str] = {
    # Standard poses (Question 9)
    1: REFERENCE_FRONT, 2: REFERENCE_FRONT, 3: REFERENCE_FRONT,
    4: REFERENCE_PYJAMA, 5: REFERENCE_FRONT, 6: REFERENCE_PYJAMA,
    7: REFERENCE_FRONT, 8: REFERENCE_FRONT, 9: REFERENCE_FRONT,
    10: REFERENCE_BACK, 11: REFERENCE_BACK,
    # Knee-length crop poses (2026-09-26) — never actually used to fetch a
    # garment reference (they skip generation entirely, see
    # image_gen.py's crop-derived-pose branch), but every pose_id in
    # to_generate is looked up here unconditionally, so an entry must exist.
    # Matches their crop-source pose's own binding.
    12: REFERENCE_FRONT, 13: REFERENCE_BACK,
    # Premium poses (Question 10)
    "P1": REFERENCE_FRONT, "P2": REFERENCE_FRONT, "P3": REFERENCE_FRONT,
    "P4": REFERENCE_FRONT, "P5": REFERENCE_FRONT, "P6": REFERENCE_FRONT,
    "P7": REFERENCE_FRONT, "P8": REFERENCE_FRONT,
    "P9": REFERENCE_BACK, "P10": REFERENCE_BACK, "P11": REFERENCE_FRONT,
    "P12": REFERENCE_FRONT, "P13": REFERENCE_BACK,
}

# The ONLY two derived poses (2026-09-26) — each maps to the ONE pose whose
# already-generated output it crops. A derived pose's source MUST also be
# present in the same request (enforced in resolve_pose_selection /
# resolve_premium_pose_selection below) — this feature never triggers a
# hidden extra API generation call, per the owner's explicit "no new AI
# generation call, no new API cost" spec.
CROP_DEPENDENCY: dict[int, int] = {12: 1, 13: 10}
PREMIUM_CROP_DEPENDENCY: dict[str, str] = {"P12": "P1", "P13": "P9"}


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
    12: Pose(
        12, "Knee-Length Front (Cropped)",
        "Framed from the top of the head down to just below the knee. This "
        "is NOT a separately generated pose — it is a direct programmatic "
        "crop of pose 1's own generated output for this product, so the "
        "stance, gesture, garment rendering, background and lighting are "
        "always identical to whatever pose 1 image was actually produced.",
        "knee_length",
        crop_source_pose=1,
    ),
    13: Pose(
        13, "Knee-Length Back (Cropped)",
        "Framed from the top of the head down to just below the knee, back "
        "view. This is NOT a separately generated pose — it is a direct "
        "programmatic crop of pose 10's own generated output for this "
        "product, so the stance, garment rendering, background and "
        "lighting are always identical to whatever pose 10 image was "
        "actually produced.",
        "knee_length",
        requires_back_reference=True,
        crop_source_pose=10,
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

    A genuine 0 (either mode: "specific" with an empty pose_numbers list,
    e.g. the owner declined Q9 with "no"/"none"/"skip" to use premium poses
    only, OR mode: "count" with count 0 — bot.ai's LLM parse of a decline
    isn't perfectly consistent about which of these two equivalent shapes
    it emits, see 2026-09-16 fix) now genuinely means ZERO standard poses
    in both branches — this function must never silently force a minimum
    of 1. Whether that's fine depends entirely on the caller: it's fine if
    the owner also picked premium poses, or explicitly meant to generate
    nothing standard; bot/handlers/new_product.py is what tells the owner
    "couldn't resolve any poses" if BOTH standard and premium end up empty.
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
        candidate_set = set(candidates)
        selected: list[int] = []
        blocked: list[tuple[int, str]] = []
        for pose_id in candidates:
            reason = hard_block_reason(pose_id)
            if reason is None:
                dep = CROP_DEPENDENCY.get(pose_id)
                if dep is not None and dep not in candidate_set:
                    reason = (
                        f"pose {pose_id} is a crop of pose {dep}'s own "
                        f"output, not generated separately — pose {dep} "
                        "must also be selected in this same request"
                    )
            if reason:
                blocked.append((pose_id, reason))
            else:
                selected.append(pose_id)
        return selected, blocked

    # No forced floor — a genuine 0 (e.g. the owner declined Q9 to use
    # premium poses only) must resolve to zero standard poses, not silently
    # get bumped up to 1. Only guards against a nonsensical negative value.
    count = max(0, pose_request["count"])
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
    # Added for the 7-category expansion (2026-09) — used only when
    # "Premium" is the sole audience-relevant category on a STANDARD-pose
    # (Question 9) generation. This is distinct from PREMIUM_MODEL_SPEC
    # below, which is the fixed editorial-model description used for the 8
    # premium POSES (Question 10) regardless of which category was picked.
    "premium": (
        "Adult Indian woman aged 25-35, poised and elegant, medium-fair "
        "complexion, natural minimal makeup."
    ),
}


def resolve_model_age_bucket(categories: list[str]) -> str:
    """Question 5's category answer -> MODEL_AGE bucket.

    "All three" is a deliberate special case that overrides the general
    "Nani wins" rule below it — per the owner's spec, "For Nani/Dadi"
    alongside exactly one other AUDIENCE category still generates the Nani
    version (that's the audience most needing representation), but naming
    all three together is its own distinct blended age instead.

    Non-audience categories (Kurtis, Kurti Sets, On Sale) never affect model
    age — they're just "which nav tab", not "who's it for" — and are
    ignored here entirely. "Premium" is the one exception: it has its own
    age bucket (25-35), used only when no audience category was also
    picked (an audience category always takes precedence, same "Nani wins"
    logic as before the 7-category expansion).
    """
    cats = {c.strip().lower() for c in categories}
    # "for nani" kept as a legacy alias in case older stored drafts/tests
    # still use the pre-rename wording.
    has_nani = "for nani/dadi" in cats or "for nani" in cats
    has_mom = "for mom" in cats
    has_me = "for me" in cats
    if has_nani and has_mom and has_me:
        return "all_three"
    if has_nani:
        return "nani"
    if has_mom and has_me:
        return "me_mom"
    if has_mom:
        return "mom"
    if has_me:
        return "me"
    if "premium" in cats:
        return "premium"
    return "me"


# --- Section B: listing-type rule (the fix for the "without pyjama"
# hallucination, carried forward from v1) ------------------------------------


def _indefinite_article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _listing_rule(listing_type: str, color: str, has_pyjama_reference: bool = False) -> str:
    if listing_type == "kurti_pyjama_set":
        article = _indefinite_article(color)
        rule = (
            f"This listing is {article} {color} kurti with its matching "
            "pyjama, salwar, or palazzo, exactly as shown together in the "
            "raw reference photos. The model wears the full set."
        )
        if has_pyjama_reference:
            # PYJAMA_REFERENCE was supplied (bot/handlers/new_product.py's
            # pyjama upload flow) — the real pyjama has its own fabric, cut
            # and embroidery, different from the kurti's, so it must be
            # rendered from that actual photo rather than guessed/matched
            # to the kurti. This is a hard override, not a suggestion.
            rule += (
                " A SEPARATE real photo of the actual pyjama/bottom is also "
                "supplied as its own reference image (PYJAMA_REFERENCE) — "
                "render the pyjama/bottom to match that reference photo "
                "EXACTLY: identical fabric, colour, cut, hem, and "
                "embroidery. Do not invent the pyjama and do not reuse the "
                "kurti's own fabric or embroidery for it — the pyjama "
                "reference photo is the ground truth for the bottom, not "
                "the kurti photos."
            )
        return rule
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
    has_pyjama_reference: bool = False,
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
        listing_rule=_listing_rule(listing_type, color, has_pyjama_reference),
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


# =============================================================================
# PREMIUM EDITORIAL POSES (Question 10, 2026-09) — a second, separate pose
# library from the 11-pose POSES dict above. Selectable for ANY category,
# not only a product tagged "Premium" in Question 5. Additive: nothing above
# this line changes behaviour when Question 10 is skipped.
# =============================================================================

# --- Premium background sets (Part 5) --------------------------------------
# 3 lettered sets — a separate namespace from BACKGROUND_PRESETS ("A"/"B"
# above), never mixed with it. Locked once per product (like
# BACKGROUND_PRESETS) but rotated A -> B -> C -> A across products instead
# of just A/B, and tracked as its own persisted state key
# (state.next_premium_background_set) so it doesn't perturb the standard
# flow's A/B alternation.

PREMIUM_BACKGROUND_SETS = {
    "A": (
        "Warm mustard-ochre textured plaster wall with soft tonal "
        "variation. A large weathered terracotta pot with handles stands "
        "on the left. On the right, three wooden picture frames of "
        "different sizes lean against the wall, each displaying a framed "
        "chikankari fabric swatch in deep jewel tones. The floor is "
        "covered with a faded vintage Persian-style carpet in cream, red "
        "and navy. Warm soft directional lighting with a gentle shadow on "
        "the wall."
    ),
    "B": (
        "Warm ochre and mustard walls with subtle vertical tonal banding. "
        "A tall rounded archway forms the centre of the frame. Visible "
        "through the arch: a carved light-wood chair with a red-gold "
        "cushion, a small round wooden side table holding a few small "
        "objects, and a muted patterned rug on the floor. Soft warm "
        "lighting, shallow depth of field on the background."
    ),
    "C": (
        "Warm mustard-yellow plaster wall with a wooden louvred shuttered "
        "window, one shutter open, bright soft daylight streaming through. "
        "Below the window, a built-in seat with a pale cream mattress and "
        "pale cushions, plus one large round tufted bolster cushion in a "
        "rich accent colour. A terracotta-toned plinth below the seat and "
        "a woven rug on the floor in the foreground."
    ),
}


def pick_premium_background_set() -> str:
    """Call ONCE per product, only if that product generates at least one
    premium pose. Every premium image for that product reuses this exact
    literal set text (zero drift), per Part 5's locking rule — this
    intentionally overrides any individual pose's own "suggested" set
    (e.g. pose P4's text mentions Set B) so a whole product's premium shoot
    reads as one consistent location, not a mix of rooms."""
    return state.next_premium_background_set()


# --- Premium model specification (Part 6) -----------------------------------
# Fixed editorial-model description used for EVERY premium pose, regardless
# of which Question 5 category the product belongs to — distinct from
# MODEL_AGE_DESCRIPTIONS above, which only drives the 11 standard poses.

PREMIUM_MODEL_SPEC = (
    "Indian woman aged 25 to 35. Poised, confident, aspirational — an "
    "editorial fashion model, not a basic catalogue model. Long dark brown "
    "wavy hair worn loose, falling naturally over the shoulders. Polished "
    "warm-toned makeup with defined brows, subtle warm eyeshadow, natural "
    "lip. Calm assured expression. Styling: stacked oxidised silver bangles "
    "or heavy silver kadas on one or both wrists, silver or oxidised jhumka "
    "earrings, occasionally a statement ring. Embroidered or metallic "
    "juttis, or barefoot for the floor-seated pose."
)

# A light variety knob layered on top of PREMIUM_MODEL_SPEC so different
# products don't all get a visually identical editorial model, without
# touching the hardcoded spec text itself or any individual pose's own
# hair/jewelry description (Part 4's pose text is authoritative where it
# specifies something explicitly).
PREMIUM_SKIN_TONES = ["warm wheatish", "fair", "deep tan", "medium olive-toned"]
PREMIUM_HAIR_TEXTURES = ["soft waves", "loose curls", "sleek straight-to-wavy"]


def pick_premium_model_variation() -> dict:
    """Call ONCE per product, only if that product generates at least one
    premium pose. Fixed across every premium image of that product (same
    convention as pick_product_identity for the standard flow)."""
    return {
        "skin_tone": random.choice(PREMIUM_SKIN_TONES),
        "hair_texture": random.choice(PREMIUM_HAIR_TEXTURES),
    }


def _premium_model_identity(variation: dict, has_face_reference: bool) -> str:
    if has_face_reference:
        identity_clause = (
            " The model's face, skin tone, hair, and jewellery must exactly "
            "match the model shown in the additional reference image — this "
            "is the same person, in the same accessories, just a different "
            "pose. Never swap in a different face partway through this "
            "product's premium photos."
        )
    else:
        # First premium image of this product — no reference to lock to
        # yet, but still explicitly told to look distinct from other
        # products' premium shoots per Part 6 ("do not reuse the same
        # woman across the premium catalogue").
        identity_clause = (
            " This is a NEW model for this product's premium shoot — do "
            "not reuse the same face, features, or styling as any other "
            "product's premium photoshoot."
        )
    return (
        f"{PREMIUM_MODEL_SPEC} Skin tone for this shoot: {variation['skin_tone']}. "
        f"Hair texture: {variation['hair_texture']}, styled as described in "
        "the pose below." + identity_clause
    )


# --- Premium micro-variation (Part 3, 2026-09-16) ---------------------------
# The 11 premium poses stay HARDCODED — base pose, crop, framing and camera
# angle never change. Only these layer on top, randomised per image, mirroring
# the standard flow's pick_variation but with premium's own vocabulary and a
# separate used-combo set (bot/image_gen.py tracks it independently from the
# standard flow's). Explicitly NOT applied to the 11 standard poses — they
# already have their own variation system above.

PREMIUM_HAND_POSITIONS = [
    "arm relaxed at the side",
    "hand lightly at the neckline",
    "hand resting at the hip",
    "hands loosely clasped",
    "fingers adjusting a bangle",
    "arm slightly bent, fingers relaxed",
]
PREMIUM_HEAD_DIRECTIONS = [
    "facing forward",
    "turned slightly left",
    "turned slightly right",
    "tilted gently down",
    "lifted slightly up",
]
PREMIUM_EYE_DIRECTIONS = [
    "direct at the camera",
    "looking off to the left",
    "looking off to the right",
    "cast downward",
    "softly lowered",
]
PREMIUM_EXPRESSIONS = ["neutral composed", "a faint closed-lip smile", "a soft warm smile"]
PREMIUM_JEWELRY_DETAILS = ["a single heavy kada", "stacked thin bangles", "a mixed bangle stack"]
PREMIUM_JHUMKA_SIZES = [
    "medium-sized jhumka earrings",
    "large statement jhumka earrings",
    "delicate small jhumka earrings",
]

# --- Hand-held flower prop (2026-09-26, Part 4; scope-fixed 2026-09-26) -----
# A single prop substitution applied across the premium editorial poses —
# does not touch pose, background, lighting, jewelry, or the model reference.
# Fixed list only, no open-ended flower types. P12/P13 (crop-derived from
# P1/P9) never pick their own flower — whatever flower ended up in P1's or
# P9's actual generated output is simply carried forward by the crop.
#
# Plural nouns (not "a sunflower") since the prop wording is now "3 to 5
# stems of {flower}" — see PREMIUM_MEGA_PROMPT_TEMPLATE's HAND PROP clause.
PREMIUM_HAND_PROPS = [
    "sunflowers", "red roses", "pink roses", "white roses",
    "marigolds", "tuberoses", "gerbera daisies",
]


def pick_premium_flower() -> str:
    """Call ONCE per product, only if that product generates at least one
    premium pose — same convention as pick_premium_model_variation /
    pick_premium_background_set. Fixed across every premium image of that
    product (including P12/P13, which never call this themselves — they
    inherit whatever flower is already in their crop source's pixels).

    Bug fix (2026-09-26): this used to be picked inside pick_premium_variation
    below, which runs once PER IMAGE, so a single product's premium photo set
    could show a different flower in every shot. Moved here, its own
    product-scoped pick, called once by image_gen.py the same way
    premium_model_variation already is."""
    return random.choice(PREMIUM_HAND_PROPS)


def pick_premium_variation(used_combos: set) -> dict:
    """Call once per premium image within a product. `used_combos` is a
    set[tuple[str, str, str]] of (hand, head, eye) already used for this
    product's PREMIUM images specifically — tracked separately from the
    standard flow's used_gesture_combos, mutated in place. Guarantees no two
    images of one product share the same (hand, head, gaze) combination.

    Does NOT pick the flower — that's product-scoped (pick_premium_flower
    above), not per-image."""
    for _ in range(50):
        hand = random.choice(PREMIUM_HAND_POSITIONS)
        head = random.choice(PREMIUM_HEAD_DIRECTIONS)
        eye = random.choice(PREMIUM_EYE_DIRECTIONS)
        combo = (hand, head, eye)
        if combo not in used_combos:
            used_combos.add(combo)
            break
    else:
        # Exhausted realistic combos — reuse rather than loop forever.
        used_combos.add(combo)
    return {
        "hand": hand,
        "head": head,
        "eye": eye,
        "expression": random.choice(PREMIUM_EXPRESSIONS),
        "jewelry_detail": random.choice(PREMIUM_JEWELRY_DETAILS),
        "jhumka_size": random.choice(PREMIUM_JHUMKA_SIZES),
    }


# --- Premium realism (Part 4, 2026-09-16) ------------------------------------
# Appended only to the premium prompt/negative-prompt — never applied to the
# 11 standard poses. Makes premium output read as a real studio photograph
# rather than an obviously-generated image.

PREMIUM_REALISM = (
    "Shot on a full-frame camera with an 85mm portrait lens, natural depth "
    "of field with soft background falloff. Realistic skin texture with "
    "visible pores and natural imperfection — no plastic or over-smoothed "
    "skin. Natural asymmetry in hair fall and fabric drape. Soft "
    "directional key light with a real shadow falling on the wall. Natural "
    "colour grade, no HDR, no oversaturation, no glow. Fabric behaves with "
    "real weight: creases at the elbow, natural gathering at the waist."
)

PREMIUM_NEGATIVE_ADDITIONS = (
    "plastic skin, airbrushed, CGI render, over-smoothed, artificial "
    "lighting, uncanny symmetry, glossy skin, HDR look"
)


# --- The 11 premium poses (Part 4) -------------------------------------------


@dataclass(frozen=True)
class PremiumPose:
    id: str
    label: str
    crop_type: str
    background_hint: str  # documentation only — the locked per-product set
    # (pick_premium_background_set) always wins over this suggestion; see
    # its docstring.
    description: str
    reference_filename: str
    requires_back_reference: bool = False
    # Set only for a pose derived by cropping another premium pose's
    # ALREADY-GENERATED output (2026-09-26, P12/P13) — see
    # prompts.PREMIUM_CROP_DEPENDENCY and image_gen.py's crop-derived-pose
    # branch. No reference photo exists for these (no photographer shot was
    # ever taken of them) — reference_filename below is a placeholder name
    # only, matching the field's documentary-only convention.
    crop_source_pose: str | None = None


PREMIUM_POSES: dict[str, PremiumPose] = {
    "P1": PremiumPose(
        "P1", "Full-Length Standing, Styled Set", "full-length", "Set A",
        "Model stands square to camera, full body head to feet, weight "
        "even, feet close together. Both hands clasped loosely in front at "
        "waist level. Chin level, calm confident expression, direct eye "
        "contact. Long dark wavy hair loose, falling over both shoulders. "
        "Stacked oxidised silver bangles on both wrists. Silver jhumka "
        "earrings. Embroidered juttis.",
        "premium_p1_fulllength_styled_set.png",
    ),
    "P2": PremiumPose(
        "P2", "Waist-Up, Looking to the Side", "waist-up",
        "Set A, wall only",
        "Cropped from above the head to just below the hip. Body angled "
        "slightly, head turned to look off-frame at roughly 45 degrees, "
        "chin lifted a little, composed expression. Hair swept to one side "
        "falling over one shoulder. One arm relaxed straight down, the "
        "other slightly bent with fingers loose. Heavy oxidised silver "
        "cuff bangles visible on both wrists.",
        "premium_p2_waistup_looking_side.png",
    ),
    "P3": PremiumPose(
        "P3", "Leaning on Wall, Hand in Hair", "waist-up / three-quarter torso",
        "Set A or B, wall corner",
        "Cropped from above the head to mid-torso. Model leans her "
        "shoulder against a wall edge or pillar corner. One arm raised, "
        "hand tucking hair behind the ear near the temple. Other arm "
        "relaxed down, resting across the body. Direct gaze, soft "
        "confident expression. Long silver jhumka earrings. Silver cuff "
        "bangles. Warm directional light from one side casting a soft "
        "shadow on the wall.",
        "premium_p3_leaning_hand_in_hair.png",
    ),
    "P4": PremiumPose(
        "P4", "Full-Length in Arched Doorway",
        "full-length, architectural framing", "Set B",
        "Model stands inside an arched doorway, full body in frame, "
        "centred in the arch. Hands clasped in front at waist. Dupatta "
        "draped over one shoulder falling to floor length. Calm direct "
        "gaze. Behind her through the arch: a carved wooden chair with a "
        "cushion, a small round wooden side table, a patterned rug. Soft "
        "depth of field so the background furniture is gently blurred.",
        "premium_p4_fulllength_archway.png",
    ),
    "P5": PremiumPose(
        "P5", "Leaning on Pillar, Hands Clasped", "waist-up",
        "Set B, pillar edge with warm gradient wall",
        "Cropped from above the head to hip. Model stands beside and "
        "leaning lightly against a pillar or wall edge that fills the left "
        "or right third of the frame. Body angled, both hands clasped low "
        "in front. Head straight or tilted very slightly, soft direct "
        "gaze. Dupatta worn draped around the neck like a scarf. Long "
        "silver jhumka earrings, heavy silver cuff bangles.",
        "premium_p5_leaning_pillar_hands_clasped.png",
    ),
    "P6": PremiumPose(
        "P6", "Reclining on Window Seat", "full-length horizontal composition",
        "Set C",
        "Full-length reclining shot. Model reclines along a built-in "
        "window seat / daybed with pale cushions, upper body propped up "
        "and leaning against a large round bolster cushion. Legs extended "
        "along the seat, garment fabric spread out to show the full drape "
        "and hem embroidery. One hand resting on the bolster, the other "
        "relaxed. Direct gaze, relaxed confident expression. Wooden "
        "shuttered window behind with bright daylight coming through. "
        "Juttis placed on the floor in the foreground. Stacked bangles.",
        "premium_p6_reclining_window_seat.png",
    ),
    "P7": PremiumPose(
        "P7", "Seated Close-Up with Bolster Cushion", "three-quarter seated",
        "Set C",
        "Closer crop of the window seat scene, roughly from above the head "
        "to mid-thigh. Model seated, one arm draped over the large round "
        "bolster cushion in the foreground, body turned toward camera. "
        "Head slightly tilted, direct gaze. Long hair falling forward over "
        "one shoulder. Long silver jhumka earrings, stacked silver bangles "
        "on the draped arm. Wooden shutters and warm wall behind.",
        "premium_p7_seated_bolster_closeup.png",
    ),
    "P8": PremiumPose(
        "P8", "Seated on Floor, Hand on Cheek", "full seated figure",
        "Set A, floor level",
        "Model seated on a vintage patterned carpet on the floor. Legs "
        "folded to one side, one knee raised, barefoot with the other leg "
        "extended. Right elbow rests on the raised knee with the hand "
        "supporting the cheek. Left hand planted flat on the carpet behind "
        "for support. Head tilted slightly, direct gaze, calm expression. "
        "Garment spread across the floor showing hem and border "
        "embroidery. Stacked oxidised silver bangles on both wrists, "
        "jhumka earrings.",
        "premium_p8_seated_floor_hand_on_cheek.png",
    ),
    "P9": PremiumPose(
        "P9", "Back Full-Length", "full-length", "locked premium set",
        "Model faces fully away from camera, standing straight and "
        "centred, full body head to feet in frame. Weight even, feet close "
        "together, both arms relaxed straight down at the sides. Long dark "
        "wavy hair worn loose, falling down the back, parted so the back "
        "yoke embroidery stays visible. Stacked oxidised silver bangles "
        "visible on one wrist at her side. Silver jhumka earrings catching "
        "the light at the side of the head. Embroidered juttis on the "
        "floor. The full back of the garment fills the frame: back yoke "
        "motifs, sleeve cuff embroidery, hem border, and the bottom's "
        "motifs and hem detail all clearly legible.",
        "premium_p9_back_fulllength.png",
        requires_back_reference=True,
    ),
    "P10": PremiumPose(
        "P10", "Back Over-the-Shoulder", "waist-up, back three-quarter",
        "locked premium set",
        "Cropped from above the head to roughly hip level. Model's back is "
        "toward camera, body angled slightly, head turned in soft profile "
        "looking back over one shoulder with a calm, faint smile. Hair "
        "swept to the opposite shoulder so the back neckline and upper "
        "yoke embroidery stay exposed. One arm relaxed at the side, the "
        "other slightly bent with fingers loose. Heavy oxidised silver "
        "cuff bangles visible. Long silver jhumka earring in profile "
        "against the jawline.",
        "premium_p10_back_over_shoulder.png",
        requires_back_reference=True,
    ),
    "P11": PremiumPose(
        "P11", "Embroidery Close-Up", "detail / torso close-up",
        "locked premium set, softly blurred",
        "Tight crop from just below the nose down to hip level. The face "
        "is deliberately cut above the lips — only the chin, jawline and "
        "earrings are in frame. Body square or very slightly angled to "
        "camera. The frame fills with the neckline yoke embroidery: motif "
        "density, thread texture, stitch detail, fabric weave. Sleeve cuff "
        "embroidery visible on both arms. Side slit visible at the hem "
        "edge of the crop. One arm relaxed straight down, the other "
        "slightly bent with a silver bangle at the wrist. Long silver "
        "jhumka earrings hanging beside the jaw. Soft directional light "
        "raking across the fabric so the embroidery casts subtle relief "
        "shadow. Background softly blurred — the garment is the subject.",
        "premium_p11_embroidery_closeup.png",
    ),
    "P12": PremiumPose(
        "P12", "Knee-Length Front (Cropped)", "knee-length", "locked premium set",
        "Framed from the top of the head down to just below the knee. This "
        "is NOT a separately generated pose — it is a direct programmatic "
        "crop of P1's own generated output for this product, so the "
        "stance, gesture, hand prop, garment rendering, background and "
        "lighting are always identical to whatever P1 image was actually "
        "produced.",
        "premium_p12_kneelength_front_cropped.png",
        crop_source_pose="P1",
    ),
    "P13": PremiumPose(
        "P13", "Knee-Length Back (Cropped)", "knee-length, back view",
        "locked premium set",
        "Framed from the top of the head down to just below the knee, back "
        "view. This is NOT a separately generated pose — it is a direct "
        "programmatic crop of P9's own generated output for this product, "
        "so the stance, garment rendering, background and lighting are "
        "always identical to whatever P9 image was actually produced.",
        "premium_p13_kneelength_back_cropped.png",
        requires_back_reference=True,
        crop_source_pose="P9",
    ),
}


def resolve_premium_pose_selection(
    pose_ids: list[str],
    has_back_reference: bool,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Question 10's premium pose numbers (e.g. ["P1", "P6", "P9"]) -> the
    validated, de-duplicated, order-preserved list of ids to generate, plus
    any blocked ids with a reason (same shape as resolve_pose_selection).

    None of the 11 premium poses require a bottom garment, and they're
    explicitly usable for ANY category/listing type per the spec. P9/P10
    (added 2026-09-16) DO require a back reference, same as standard poses
    10/11 — never silently dropped here (the owner named it on purpose), so
    a genuinely missing back reference is returned as blocked for the
    caller to surface, exactly like resolve_pose_selection's specific mode."""
    normalized_ids = {raw.strip().upper() for raw in pose_ids}
    selected: list[str] = []
    blocked: list[tuple[str, str]] = []
    for raw in pose_ids:
        pid = raw.strip().upper()
        if pid not in PREMIUM_POSES or pid in selected:
            continue
        pose = PREMIUM_POSES[pid]
        if pose.requires_back_reference and not has_back_reference:
            blocked.append((pid, "no back-side raw reference photo was supplied"))
            continue
        dep = PREMIUM_CROP_DEPENDENCY.get(pid)
        if dep is not None and dep not in normalized_ids:
            blocked.append((
                pid,
                f"pose {pid} is a crop of {dep}'s own output, not generated "
                f"separately — {dep} must also be selected in this same "
                "request",
            ))
            continue
        selected.append(pid)
    return selected, blocked


PREMIUM_MEGA_PROMPT_TEMPLATE = """\
{safety_rules}

MODEL: {model_identity}

GARMENT: Photorealistic e-commerce fashion photo of the model wearing the \
exact {color} kurti shown in the raw reference photos, made of {material}, \
with the same chikankari embroidery detail, fabric texture, and {color} \
colour faithfully reproduced. {length_rule}

LISTING TYPE: {listing_rule}

PREMIUM EDITORIAL POSE {pose_id} — {pose_label}: {pose_description}

GESTURE FOR THIS IMAGE: {hand}; head {head}; eyes {eye}; expression: \
{expression}; jewellery detail: {jewelry_detail}; {jhumka_size}.

HAND PROP: the model holds the flowers in a relaxed, editorial way, \
naturally near the chest — either cradling the blooms gently with one \
hand resting just below the collarbone, or with both hands resting \
together around the stems, whichever reads more natural for the gesture \
above; vary this hand placement across the product's different poses the \
way a real photoshoot would, while the flower type itself stays identical \
in every image. This should read as a warm, natural, premium lifestyle/ \
editorial photograph — not a plain product photo with a flower placed on \
top. A small, casual handful of 3 to 5 stems of {flower}, all the same \
flower type, with their long green stems/stalks visible, held loosely — \
like flowers freshly picked or received, NOT a florist-arranged bouquet \
and NOT wrapped in paper, cellophane, or ribbon. If the gesture above \
already occupies both hands with a specific task (for example resting on \
a cushion, supporting the cheek, or braced on the floor for balance), let \
the stems rest loosely across the fingers of one of those hands without \
changing its described position or task in any other way.

BACKGROUND (locked for this entire product, identical in every image): \
{background}

LIGHTING: {lighting}

GARMENT PRESENTATION: {cleanup_rule}

FIDELITY: Do not invent, guess, or mirror any part of the garment that is \
not visible in the reference photos — render only what is actually shown \
there.
{back_fidelity_block}
REALISM: {realism}

DO NOT INCLUDE ANY OF: {negative_prompt}

OUTPUT: {output_spec}
"""


def build_premium_pose_prompt(
    *,
    pose_id: str,
    color: str,
    material: str,
    kurti_length: str,
    listing_type: str,
    background_set: str,
    model_variation: dict,
    variation: dict,
    flower: str,
    has_face_reference: bool,
    has_pyjama_reference: bool = False,
    back_reference_description: str | None = None,
) -> str:
    pose = PREMIUM_POSES[pose_id]

    length_rule = (
        "The kurti is SHORT length (hip to mid-thigh) — render the "
        "proportions as a short kurti, not a long one."
        if kurti_length == "short"
        else "The kurti is LONG length (knee-length or longer) — render "
        "the full correct length."
    )

    negative_prompt = f"{NEGATIVE_PROMPT}, {PREMIUM_NEGATIVE_ADDITIONS}"
    back_fidelity_block = ""
    if pose.requires_back_reference:
        negative_prompt = f"{negative_prompt}, {BACK_VIEW_NEGATIVE_ADDITIONS}"
        back_fidelity_block = "\n" + BACK_VIEW_FIDELITY.format(
            back_reference_description=back_reference_description
            or "(no separate description available — rely on the back "
            "reference image alone, and still follow every rule above.)"
        ) + "\n"

    return PREMIUM_MEGA_PROMPT_TEMPLATE.format(
        safety_rules=SAFETY_RULES,
        model_identity=_premium_model_identity(model_variation, has_face_reference),
        color=color,
        material=material,
        length_rule=length_rule,
        listing_rule=_listing_rule(listing_type, color, has_pyjama_reference),
        pose_id=pose.id,
        pose_label=pose.label,
        pose_description=pose.description,
        hand=variation["hand"],
        head=variation["head"],
        eye=variation["eye"],
        expression=variation["expression"],
        jewelry_detail=variation["jewelry_detail"],
        jhumka_size=variation["jhumka_size"],
        flower=flower,
        background=PREMIUM_BACKGROUND_SETS[background_set],
        lighting=LIGHTING,
        cleanup_rule=GARMENT_CLEANUP,
        back_fidelity_block=back_fidelity_block,
        realism=PREMIUM_REALISM,
        negative_prompt=negative_prompt,
        output_spec=OUTPUT_SPEC,
    )
