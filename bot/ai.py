import base64
import json

from openai import AsyncOpenAI

from bot.config import OPENAI_API_KEY

_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Flagship for the vision/analysis turn — this is the one place accuracy on
# "don't guess" actually matters, worth the cost. A cheaper model handles
# plain text generation below.
_VISION_MODEL = "gpt-5.6"
_TEXT_MODEL = "gpt-5.6-luna"

_ANALYZE_PHOTOS_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "garment_visual_analysis",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "garment_type": {
                    "type": "string",
                    "description": "e.g. kurta, kurti, dress — only what's visually identifiable",
                },
                "dominant_color": {"type": "string"},
                "embroidery_or_pattern_description": {
                    "type": "string",
                    "description": (
                        "Plain description of visible embroidery/print style, e.g. "
                        "'white floral chikankari embroidery on the yoke and sleeves'"
                    ),
                },
            },
            "required": [
                "garment_type",
                "dominant_color",
                "embroidery_or_pattern_description",
            ],
            "additionalProperties": False,
        },
    },
}

_ANALYZE_SYSTEM_PROMPT = (
    "You are looking at raw product photos of a garment for an ecommerce listing. "
    "Describe ONLY what is visually verifiable in the photos: garment type, dominant "
    "colour, and the embroidery/pattern style you can see. "
    "Never guess or infer price, fabric composition, size, or stock quantity — those "
    "are not visually verifiable and are not part of this task."
)


async def analyze_photos(photo_bytes_list: list[bytes]) -> dict:
    content: list[dict] = [{"type": "text", "text": "Analyze these product photos."}]
    for photo_bytes in photo_bytes_list:
        b64 = base64.b64encode(photo_bytes).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        )

    response = await _client.chat.completions.create(
        model=_VISION_MODEL,
        messages=[
            {"role": "system", "content": _ANALYZE_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        response_format=_ANALYZE_PHOTOS_SCHEMA,
    )
    return json.loads(response.choices[0].message.content)


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


async def generate_description(
    garment_type: str, fabric: str, dominant_color: str, embroidery_description: str
) -> dict:
    prompt = (
        f"Write an ecommerce product title and description for a {dominant_color} "
        f"{garment_type} made of {fabric}, featuring {embroidery_description}. "
        "This is a premium handcrafted Lucknowi Chikankari piece from the brand "
        "Rama Chikan. Return a short, appealing title (under 70 characters) and a "
        "2-3 sentence HTML description highlighting the craftsmanship."
    )
    response = await _client.chat.completions.create(
        model=_TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format=_PRODUCT_COPY_SCHEMA,
    )
    return json.loads(response.choices[0].message.content)
