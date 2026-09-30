import base64
import io
import json

from openai import AsyncOpenAI
from PIL import Image

from bot.config import OPENAI_API_KEY

_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

_TEXT_MODEL = "gpt-5.6-luna"
# Flagship for the one visually-verifiable fact we actually need from the raw
# photos (garment colour) — worth the accuracy here, everything else in the
# flow is owner-entered rather than guessed.
_VISION_MODEL = "gpt-5.6"

# Cost optimization (2026-09-26) — vision-model cost scales with image
# resolution, and NOT every vision call here needs full resolution to do its
# job correctly. Applied ONLY to detect_color (dominant colour is a coarse,
# low-frequency property, trivially readable at low resolution) and
# generate_instagram_caption (pure marketing text, has zero effect on the
# actual product photos). Deliberately NOT applied to describe_back_reference
# — that call counts and locates fine embroidery motifs, and its output text
# is fed directly into what the back-view POSE IMAGES actually draw
# (prompts.BACK_VIEW_FIDELITY) — downsizing that one risks a real accuracy
# loss in generated images, which is exactly what this change must not do.
_VISION_ANALYSIS_MAX_DIMENSION = 512


def _downscale_for_analysis(image_bytes: bytes) -> bytes:
    """Shrinks a copy of the image for a coarse vision-analysis call only —
    never touches the original bytes used anywhere else (image generation,
    Shopify upload, Instagram posting all keep the full-resolution image)."""
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    longest = max(width, height)
    if longest > _VISION_ANALYSIS_MAX_DIMENSION:
        scale = _VISION_ANALYSIS_MAX_DIMENSION / longest
        image = image.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()

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
        b64 = base64.b64encode(_downscale_for_analysis(photo_bytes)).decode("ascii")
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


_KURTI_ANSWER_PARSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "kurti_answers",
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
                            "P9", "P10", "P11", "P12", "P13",
                        ],
                    },
                },
            },
            "required": [
                "material", "sizes", "price", "discount_pct", "categories", "is_bestseller",
                "kurti_length", "pose_request", "premium_pose_numbers",
            ],
            "additionalProperties": False,
        },
    },
}


async def parse_kurti_answers(text: str) -> dict:
    """Buttons 1 and 2 (Kurti / Kurti + Pyjama Set, 2026-09-29 four-button
    flow restructure) — identical 9-question set for both; which photos got
    collected (and therefore kurti_only vs kurti_pyjama_set) was already
    decided by which button the owner tapped, not asked here. This is the
    old parse_new_product_answers minus the old Q8 (listing_type) field —
    everything else is unchanged in meaning."""
    prompt = (
        "Extract structured answers from this shopkeeper's reply to 9 "
        "questions (material, sizes with quantity, price, discount percent, "
        "category/categories, whether this is a bestseller, kurti length, "
        "a pose request, and a premium pose request). "
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
        "that answer. "
        "For the pose-numbers question: if the reply lists specific pose "
        "numbers (e.g. '1, 5, 3' or '1 5 3' or 'poses 2 and 7'), set mode to "
        "'specific' and pose_numbers to that list of integers (count can be 0). "
        "If the reply says 'all poses' (meaning all 13), set mode to 'specific' "
        "and pose_numbers to [1,2,3,4,5,6,7,8,9,10,11,12,13]. If the reply is just a "
        "single number with no list context (e.g. '4' meaning 'give me 4 "
        "images'), set mode to 'count' and count to that integer (pose_numbers "
        "can be empty). If the reply DECLINES standard poses for this question "
        "(e.g. 'no', 'none', 'skip' — typically because the owner only wants "
        "premium poses instead), set mode to 'specific' and "
        "pose_numbers to an empty list (count can be 0) — this means ZERO "
        "standard poses, not 'pick one for me'. Pose numbers are always "
        "between 1 and 13 (poses 12 and 13 are knee-length crops that only "
        "work if the reply also includes pose 1 or pose 10 respectively — "
        "extract exactly what the reply says either way, don't add or "
        "remove poses yourself). "
        "For the premium pose-numbers question: if the reply names "
        "specific premium poses (e.g. 'P1, P6, P9' or 'premium 1, 6, 9'), set "
        "premium_pose_numbers to that list of strings in the form 'P1'..'P13'. "
        "If the reply says 'all premium' (meaning all 13 premium poses), set "
        "premium_pose_numbers to ['P1','P2','P3','P4','P5','P6','P7','P8','P9','P10','P11','P12','P13']. "
        "If the reply skips this question, says 'skip', 'none', or 'no', set "
        "premium_pose_numbers to an empty list.\n\n"
        "Reply:\n" + text
    )
    response = await _client.chat.completions.create(
        model=_TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format=_KURTI_ANSWER_PARSE_SCHEMA,
    )
    return json.loads(response.choices[0].message.content)


_DUPATTA_ANSWER_PARSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "dupatta_answers",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "material": {"type": "string"},
                "length": {"type": "string", "enum": ["2.25m", "2.50m", "2.75m"]},
                "price": {"type": "number"},
                "discount_pct": {"type": "number"},
                "is_bestseller": {"type": "boolean"},
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
                "material", "length", "price", "discount_pct",
                "is_bestseller", "pose_request",
            ],
            "additionalProperties": False,
        },
    },
}


async def parse_dupatta_answers(text: str) -> dict:
    """Button 3 (Dupatta, 2026-09-30). 6 questions: material, length, price,
    discount, bestseller, poses (5-pose menu, no premium tier). No category
    question — every product from this flow is fixed to the Dupatta
    collection, handled by the caller, not extracted here (2026-09-30: the
    category question was removed at the owner's request since Dupattas
    already have their own dedicated storefront collection)."""
    prompt = (
        "Extract structured answers from this shopkeeper's reply to 6 "
        "questions about a dupatta listing (material, length, price, "
        "discount percent, whether this is a bestseller, and a pose "
        "request). "
        "length must be exactly one of '2.25m', '2.50m', or '2.75m' — the "
        "reply may give it as a plain number like '2.25' or '2.5', match it "
        "to the closest of those three exact values. "
        "If no discount is mentioned, discount_pct is 0. "
        "is_bestseller is true only if the reply clearly says "
        "yes/bestseller/best-selling, false for no/not mentioned. "
        "For the pose-numbers question: if the reply lists specific pose "
        "numbers (e.g. '1, 3' or '1 3'), set mode to 'specific' and "
        "pose_numbers to that list of integers (count can be 0). If the "
        "reply says 'all poses' (meaning all 5), set mode to 'specific' and "
        "pose_numbers to [1,2,3,4,5]. If the reply is just a single number "
        "with no list context (e.g. '3' meaning 'give me 3 images'), set "
        "mode to 'count' and count to that integer. Pose numbers are always "
        "between 1 and 5.\n\n"
        "Reply:\n" + text
    )
    response = await _client.chat.completions.create(
        model=_TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format=_DUPATTA_ANSWER_PARSE_SCHEMA,
    )
    return json.loads(response.choices[0].message.content)


_BOTTOMS_ANSWER_PARSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "bottoms_answers",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "material": {"type": "string"},
                "garment_type": {
                    "type": "string",
                    "enum": ["Pant", "Plazo", "Balloon Salwar", "Tulip Salwar", "Sharara"],
                },
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
                "is_bestseller": {"type": "boolean"},
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
                "material", "garment_type", "sizes", "price", "discount_pct",
                "is_bestseller", "pose_request",
            ],
            "additionalProperties": False,
        },
    },
}


async def parse_bottoms_answers(text: str) -> dict:
    """Button 4 (Women Bottoms, 2026-09-29). 6 questions: material & type,
    sizes+qty, price, discount, bestseller, poses (5-pose menu). No category
    question — every product from this flow is fixed to the Women Bottoms
    collection, handled by the caller, not extracted here."""
    prompt = (
        "Extract structured answers from this shopkeeper's reply to 6 "
        "questions about a women's bottoms listing (material and garment "
        "type, sizes with quantity, price, discount percent, whether this "
        "is a bestseller, and a pose request). "
        "The first question gives BOTH the fabric material and the garment "
        "type together (e.g. 'Chiffon, Sharara' or 'cotton plazo'). "
        "garment_type must be matched to exactly one of: 'Pant', 'Plazo', "
        "'Balloon Salwar', 'Tulip Salwar', 'Sharara' — the reply may use "
        "close variants (e.g. 'palazzo' -> 'Plazo', 'balloon' -> 'Balloon "
        "Salwar', 'tulip' -> 'Tulip Salwar'). material is whatever fabric "
        "word(s) remain (e.g. 'Chiffon'). "
        "Normalize every size to one of exactly: XS, S, M, L, XL, XXL, 3XL. "
        "If no discount is mentioned, discount_pct is 0. "
        "is_bestseller is true only if the reply clearly says "
        "yes/bestseller/best-selling, false for no/not mentioned. "
        "For the pose-numbers question: if the reply lists specific pose "
        "numbers (e.g. '1, 3' or '1 3'), set mode to 'specific' and "
        "pose_numbers to that list of integers (count can be 0). If the "
        "reply says 'all poses' (meaning all 5), set mode to 'specific' and "
        "pose_numbers to [1,2,3,4,5]. If the reply is just a single number "
        "with no list context (e.g. '3' meaning 'give me 3 images'), set "
        "mode to 'count' and count to that integer. Pose numbers are always "
        "between 1 and 5.\n\n"
        "Reply:\n" + text
    )
    response = await _client.chat.completions.create(
        model=_TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format=_BOTTOMS_ANSWER_PARSE_SCHEMA,
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


# listing_type -> the noun phrase used in generated copy (description,
# Instagram caption). Extended 2026-09-29 for Buttons 3/4 (Dupatta, Women
# Bottoms) — kurti_only/kurti_pyjama_set entries and their fallback are
# byte-identical to the pre-restructure behaviour.
_GARMENT_TYPE_PHRASES = {
    "kurti_pyjama_set": "kurti and pyjama set",
    "kurti_only": "kurti",
    "dupatta": "dupatta",
    "women_bottoms": "pair of women's bottoms",
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
    garment_type = _GARMENT_TYPE_PHRASES.get(listing_type, "kurti")
    b64 = base64.b64encode(_downscale_for_analysis(first_image_bytes)).decode("ascii")
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
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
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
    garment_type = _GARMENT_TYPE_PHRASES.get(listing_type, "kurti")
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
