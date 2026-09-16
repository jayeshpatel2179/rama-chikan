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
                        "enum": [
                            "Premium", "Kurtis", "Kurti Sets", "For Nani/Dadi",
                            "For Mom", "For Me", "On Sale",
                        ],
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
                "premium_pose_numbers": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8",
                            "P9", "P10", "P11",
                        ],
                    },
                },
            },
            "required": [
                "material", "sizes", "price", "discount_pct", "categories", "is_bestseller",
                "kurti_length", "listing_type", "pose_request", "premium_pose_numbers",
            ],
            "additionalProperties": False,
        },
    },
}


async def parse_new_product_answers(text: str) -> dict:
    """Extracts the 10 answers (material, sizes+qty, price, discount %,
    category/categories, bestseller, kurti length, listing type, pose
    request, premium pose request) from one free-text reply like:

        rayon
        3 of XS / 1 of S
        1500
        20%
        For Mom
        yes
        short
        kurti + pyjama set
        1, 5, 3
        skip

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
    this function only extracts what the owner typed.

    premium_pose_numbers captures Question 10's answer (the 11-pose premium
    editorial menu, e.g. "P1, P6, P9") — empty if the owner skipped it or
    said no/none. On Sale (categories) is NOT trusted from this extraction
    for whether the product actually goes on sale — bot/handlers/new_product.py
    derives that deterministically from discount_pct after parsing, per the
    spec's "must not appear in On Sale under any circumstance" rule when
    there's no discount."""
    prompt = (
        "Extract structured answers from this shopkeeper's reply to 10 "
        "questions (material, sizes with quantity, price, discount percent, "
        "category/categories, whether this is a bestseller, kurti length, "
        "listing type, a pose request, and a premium pose request). "
        "Normalize every size to one of exactly: XS, S, M, L, XL, XXL, 3XL. "
        "If no discount is mentioned, discount_pct is 0. "
        "For the 5th question (category): each category must be exactly one "
        "of 'Premium', 'Kurtis', 'Kurti Sets', 'For Nani/Dadi', 'For Mom', "
        "'For Me', or 'On Sale'. The reply may give these as numbers "
        "(1=Premium, 2=Kurtis, 3=Kurti Sets, 4=For Nani/Dadi, 5=For Mom, "
        "6=For Me, 7=On Sale) or as names, and may name one or several (e.g. "
        "'1, 4', 'premium and for mom', 'nani and mom', 'all three', 'all "
        "categories'). Treat 'For Nani' (without '/Dadi') and 'Kurtas' as the "
        "same thing as 'For Nani/Dadi' and 'Kurtis' respectively — the store "
        "renamed these tabs but the owner may still use the old names. "
        "Include every category the reply mentions in the categories array; "
        "don't add On Sale yourself just because a discount was given — only "
        "include it if the owner's reply to question 5 actually names it. "
        "is_bestseller is true only if the reply clearly says "
        "yes/bestseller/best-selling for that question, false for no/not "
        "mentioned. kurti_length must be exactly 'short' or 'long' based on "
        "that answer. listing_type must be exactly 'kurti_pyjama_set' unless "
        "the reply clearly says kurti only / just the kurti / no pyjama in the "
        "listing for that question, in which case it's 'kurti_only'. "
        "For the 9th question (pose request): if the reply lists specific pose "
        "numbers (e.g. '1, 5, 3' or '1 5 3' or 'poses 2 and 7'), set mode to "
        "'specific' and pose_numbers to that list of integers (count can be 0). "
        "If the reply says 'all poses' (meaning all 11), set mode to 'specific' "
        "and pose_numbers to [1,2,3,4,5,6,7,8,9,10,11]. If the reply is just a "
        "single number with no list context (e.g. '4' meaning 'give me 4 "
        "images'), set mode to 'count' and count to that integer (pose_numbers "
        "can be empty). If the reply DECLINES standard poses for this question "
        "(e.g. 'no', 'none', 'skip' — typically because the owner only wants "
        "premium poses from question 10 instead), set mode to 'specific' and "
        "pose_numbers to an empty list (count can be 0) — this means ZERO "
        "standard poses, not 'pick one for me'. Pose numbers are always "
        "between 1 and 11. "
        "For the 10th question (premium pose request): if the reply names "
        "specific premium poses (e.g. 'P1, P6, P9' or 'premium 1, 6, 9'), set "
        "premium_pose_numbers to that list of strings in the form 'P1'..'P11'. "
        "If the reply says 'all premium' (meaning all 11 premium poses), set "
        "premium_pose_numbers to ['P1','P2','P3','P4','P5','P6','P7','P8','P9','P10','P11']. "
        "If the reply skips this question, says 'skip', 'none', or 'no', set "
        "premium_pose_numbers to an empty list.\n\n"
        "Reply:\n" + text
    )
    response = await _client.chat.completions.create(
        model=_TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format=_ANSWER_PARSE_SCHEMA,
    )
    return json.loads(response.choices[0].message.content)


_INSTAGRAM_CAPTION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "instagram_caption",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"caption": {"type": "string"}},
            "required": ["caption"],
            "additionalProperties": False,
        },
    },
}


async def generate_instagram_caption(
    first_image_bytes: bytes,
    color: str,
    material: str,
    listing_type: str,
) -> str:
    """Writes the Instagram caption (Part 5, 2026-09-16) for the FIRST
    generated image of a product only — upload-post doesn't support
    carousels here, so there's only ever one image to caption per post.

    Looks at the actual generated image (not just the text fields) so the
    caption is grounded in what's actually shown, same convention as
    describe_back_reference/detect_color above rather than writing blind
    from the intake answers alone."""
    garment_type = "kurti and pyjama set" if listing_type == "kurti_pyjama_set" else "kurti"
    b64 = base64.b64encode(first_image_bytes).decode("ascii")
    prompt = (
        "You are the Instagram voice of Rama Chikan, a heritage Lucknowi "
        "chikankari brand — three generations of hand embroidery craft. "
        f"Write ONE Instagram caption for this {color} {garment_type} made "
        f"of {material}, hand-embroidered with chikankari work, shown in "
        "the attached photo.\n\n"
        "Rules:\n"
        "- 1 to 1.5 lines, maximum 2.5 lines.\n"
        "- Fashion-forward tone matching this specific kurti: mention the "
        "colour, the fabric, and the occasion it suits.\n"
        "- Warm and aspirational, matching Rama Chikan's brand voice — "
        "premium and rooted in heritage, never overhyped.\n"
        "- No emoji spam — at most one or two.\n"
        "- Must include #RamaChikanLucknow on every single post, plus 3 to "
        "4 additional relevant hashtags mixing broad reach and niche "
        "intent (e.g. #Chikankari #LucknowiChikankari #KurtiLove "
        "#EthnicWear #HandEmbroidered — pick ones that actually fit this "
        "piece, don't just reuse the examples verbatim every time).\n"
        "- Put the hashtags at the end of the caption, space-separated.\n"
        "- Do not wrap the caption in quotation marks."
    )
    response = await _client.chat.completions.create(
        model=_VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ],
        response_format=_INSTAGRAM_CAPTION_SCHEMA,
    )
    return json.loads(response.choices[0].message.content)["caption"].strip()


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
