import base64
import json

from openai import AsyncOpenAI

from bot.config import OPENAI_API_KEY

_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

_TEXT_MODEL = "gpt-5.6-luna"
# Flagship for the one visually-verifiable fact we actually need from the raw
# photos (garment colour) — worth the accuracy here, everything else in the
# flow is owner-entered rather than guessed.
_VISION_MODEL = "gpt-5.6"

_COLOR_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "garment_color",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"dominant_color": {"type": "string"}},
            "required": ["dominant_color"],
            "additionalProperties": False,
        },
    },
}


async def detect_color(photo_bytes_list: list[bytes]) -> str:
    """Identifies only the dominant garment colour from the raw photos —
    never guesses fabric/size/stock, those are always owner-entered."""
    content: list[dict] = [
        {"type": "text", "text": "What is the dominant colour of this garment?"}
    ]
    for photo_bytes in photo_bytes_list:
        b64 = base64.b64encode(photo_bytes).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        )

    response = await _client.chat.completions.create(
        model=_VISION_MODEL,
        messages=[{"role": "user", "content": content}],
        response_format=_COLOR_SCHEMA,
    )
    return json.loads(response.choices[0].message.content)["dominant_color"]


_BACK_REFERENCE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "back_reference_description",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "has_yoke_panel": {"type": "boolean"},
                "motif_count_description": {"type": "string"},
                "motif_placement": {"type": "string"},
                "embroidery_locations": {"type": "string"},
                "overall_description": {"type": "string"},
            },
            "required": [
                "has_yoke_panel", "motif_count_description", "motif_placement",
                "embroidery_locations", "overall_description",
            ],
            "additionalProperties": False,
        },
    },
}


async def describe_back_reference(back_photo_bytes: bytes) -> str:
    """Vision-describes ONLY what's actually visible on the raw back photo,
    as literal text for injection into the back-view generation prompt
    alongside the image itself.

    Added because the image reference alone was not enough to stop the
    image model inventing a yoke/motif column on back views that don't
    have one (it was picking up the FRONT's motif density instead) — see
    bot/prompts.py's back-view fidelity block, which is where this text
    gets used. Chikankari kurti backs are usually much plainer than the
    front, and that's the correct, expected answer here — this function
    must not nudge the model toward assuming otherwise."""
    b64 = base64.b64encode(back_photo_bytes).decode("ascii")
    content = [
        {
            "type": "text",
            "text": (
                "This is the RAW back-of-garment reference photo for a chikankari "
                "kurti. Describe ONLY what is actually visible on the back — do "
                "not describe the front, do not assume anything typical of "
                "chikankari kurtis in general, and do not guess. Answer "
                "factually: is there a horizontal yoke seam/panel across the "
                "upper back? How many embroidery motifs are on the main body of "
                "the back, and exactly where are they placed? Where does "
                "embroidery actually sit (e.g. sleeve cuffs, shoulder/neck edge, "
                "centre back, hem)? Chikankari kurti backs are often much "
                "plainer than the front — report exactly what you see; plain "
                "or near-plain is a valid and expected answer, not a mistake."
            ),
        },
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    response = await _client.chat.completions.create(
        model=_VISION_MODEL,
        messages=[{"role": "user", "content": content}],
        response_format=_BACK_REFERENCE_SCHEMA,
    )
    data = json.loads(response.choices[0].message.content)
    yoke_line = (
        "There IS a horizontal yoke seam/panel across the upper back."
        if data["has_yoke_panel"]
        else "There is NO yoke seam or yoke panel of any kind across the upper "
        "back — the back fabric is continuous with no horizontal seam line there."
    )
    return (
        f"{yoke_line} Motifs on the back body: {data['motif_count_description']}, "
        f"placed at: {data['motif_placement']}. Embroidery actually appears at: "
        f"{data['embroidery_locations']}. Overall: {data['overall_description']}"
    )


_PRODUCT_COPY_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "product_copy",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description_html": {"type": "string"},
            },
            "required": ["title", "description_html"],
            "additionalProperties": False,
        },
    },
}


_ANSWER_PARSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "new_product_answers",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "material": {"type": "string"},
                "sizes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "size": {"type": "string"},
                            "quantity": {"type": "integer"},
                        },
                        "required": ["size", "quantity"],
                        "additionalProperties": False,
                    },
                },
                "price": {"type": "number"},
                "discount_pct": {"type": "number"},
                "categories": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["For Nani", "For Mom", "For Me"],
                    },
                },
                "is_bestseller": {"type": "boolean"},
                "kurti_length": {"type": "string", "enum": ["short", "long"]},
                "listing_type": {
                    "type": "string",
                    "enum": ["kurti_pyjama_set", "kurti_only"],
                },
                "pose_request": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["specific", "count"]},
                        "pose_numbers": {"type": "array", "items": {"type": "integer"}},
                        "count": {"type": "integer"},
                    },
                    "required": ["mode", "pose_numbers", "count"],
                    "additionalProperties": False,
                },
            },
            "required": [
                "material", "sizes", "price", "discount_pct", "categories", "is_bestseller",
                "kurti_length", "listing_type", "pose_request",
            ],
            "additionalProperties": False,
        },
    },
}


async def parse_new_product_answers(text: str) -> dict:
    """Extracts the 9 answers (material, sizes+qty, price, discount %,
    category/categories, bestseller, kurti length, listing type, pose
    request) from one free-text reply like:

        rayon
        3 of XS / 1 of S
        1500
        20%
        For Mom
        yes
        short
        kurti + pyjama set
        1, 5, 3

    Sizes must be normalized to the store's exact size codes: XS, S, M, L,
    XL, XXL, 3XL. discount_pct is 0 if no discount was mentioned. categories
    can list more than one — the owner may push one item to several
    discovery tabs at once (e.g. "for nani and for mom", "all three").

    listing_type describes what's being SOLD (set vs kurti only), not what
    the model wears in the photo — the model always wears a real bottom
    either way (see bot/prompts.py's listing-type rule); this question was
    deliberately reworded away from "with/without pyjama" after that
    phrasing caused the image model to hallucinate bare-legged output.

    pose_request captures Question 9's answer: either specific pose numbers
    ("1, 5, 3" -> mode 'specific', pose_numbers [1,5,3]) or just a count
    ("4" -> mode 'count', count 4). The actual pose IDs to generate are
    resolved afterward by bot.prompts.resolve_pose_selection, not here —
    this function only extracts what the owner typed."""
    prompt = (
        "Extract structured answers from this shopkeeper's reply to 9 questions "
        "(material, sizes with quantity, price, discount percent, "
        "category/categories, whether this is a bestseller, kurti length, "
        "listing type, and a pose request). "
        "Normalize every size to one of exactly: XS, S, M, L, XL, XXL, 3XL. "
        "If no discount is mentioned, discount_pct is 0. Each category must be "
        "exactly 'For Nani', 'For Mom', or 'For Me' — the reply may name one, "
        "two, or all three of them (e.g. 'nani and mom', 'all three', 'all "
        "categories'); include every category the reply mentions, in the "
        "categories array. is_bestseller is true only if the reply clearly "
        "says yes/bestseller/best-selling for that question, false for "
        "no/not mentioned. kurti_length must be exactly 'short' or 'long' based "
        "on that answer. listing_type must be exactly 'kurti_pyjama_set' unless "
        "the reply clearly says kurti only / just the kurti / no pyjama in the "
        "listing for that question, in which case it's 'kurti_only'. "
        "For the 9th question (pose request): if the reply lists specific pose "
        "numbers (e.g. '1, 5, 3' or '1 5 3' or 'poses 2 and 7'), set mode to "
        "'specific' and pose_numbers to that list of integers (count can be 0). "
        "If the reply says 'all poses' (meaning all 11), set mode to 'specific' "
        "and pose_numbers to [1,2,3,4,5,6,7,8,9,10,11]. If the reply is just a "
        "single number with no list context (e.g. '4' meaning 'give me 4 "
        "images'), set mode to 'count' and count to that integer (pose_numbers "
        "can be empty). Pose numbers are always between 1 and 11.\n\n"
        "Reply:\n" + text
    )
    response = await _client.chat.completions.create(
        model=_TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format=_ANSWER_PARSE_SCHEMA,
    )
    return json.loads(response.choices[0].message.content)


async def generate_description(
    color: str,
    material: str,
    listing_type: str = "kurti_pyjama_set",
    regenerate: bool = False,
) -> dict:
    garment_type = "kurti and pyjama set" if listing_type == "kurti_pyjama_set" else "kurti"
    prompt = (
        f"Write an ecommerce product title and description for a {color} "
        f"{garment_type} made of {material}, hand-embroidered with chikankari work. "
        "This is a premium piece from Rama Chikan, a heritage Lucknowi chikankari "
        "brand — three generations of hand embroidery craft. Brand voice: warm, "
        "premium, rooted in heritage, never overhyped. The description must open by "
        f"naming the {color} colour, and must be HTML with a short headline in a "
        "<h3> tag followed by 2-3 sentences in a <p> tag highlighting the "
        "craftsmanship. Title under 70 characters."
    )
    if listing_type == "kurti_only":
        prompt += (
            " This listing is for the kurti ONLY — do not describe or imply a "
            "pyjama/bottom is included. If the product photo shows a plain "
            "bottom, add one brief closing line noting it is shown for "
            "styling reference only and is not included in this listing."
        )
    if regenerate:
        prompt += (
            " Write a fresh take, clearly different wording and headline angle "
            "from a typical first draft."
        )
    response = await _client.chat.completions.create(
        model=_TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format=_PRODUCT_COPY_SCHEMA,
    )
    return json.loads(response.choices[0].message.content)
