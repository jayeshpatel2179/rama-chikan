import mimetypes
import time

import httpx

from bot.config import SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_STORE_DOMAIN

# Shopify releases a new stable API version quarterly. Bump this when it
# goes stale (Shopify supports each version for ~1 year / 4 releases).
_API_VERSION = "2026-07"
_ADMIN_URL = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/{_API_VERSION}/graphql.json"
_TOKEN_URL = f"https://{SHOPIFY_STORE_DOMAIN}/admin/oauth/access_token"

_token_cache: dict = {"token": "", "expires_at": 0.0}
_location_id_cache: str | None = None


async def _get_access_token(client: httpx.AsyncClient) -> str:
    # Dev Dashboard apps use client_credentials, not a permanent static
    # token — refresh a little before the ~24h expiry so a request never
    # runs on a token that's about to die mid-flight.
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 120:
        return _token_cache["token"]

    response = await client.post(
        _TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
        },
    )
    response.raise_for_status()
    payload = response.json()
    _token_cache["token"] = payload["access_token"]
    _token_cache["expires_at"] = time.time() + payload.get("expires_in", 86400)
    return _token_cache["token"]


async def _graphql(query: str, variables: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _get_access_token(client)
        response = await client.post(
            _ADMIN_URL,
            json={"query": query, "variables": variables},
            headers={"X-Shopify-Access-Token": token},
        )
        response.raise_for_status()
        payload = response.json()
        if "errors" in payload:
            raise RuntimeError(f"Shopify GraphQL error: {payload['errors']}")
        return payload["data"]


_LOCATIONS_QUERY = """
query {
  locations(first: 1) {
    nodes { id }
  }
}
"""


async def get_primary_location_id() -> str:
    global _location_id_cache
    if _location_id_cache:
        return _location_id_cache
    data = await _graphql(_LOCATIONS_QUERY, {})
    _location_id_cache = data["locations"]["nodes"][0]["id"]
    return _location_id_cache


_STAGED_UPLOAD_MUTATION = """
mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters { name value }
    }
    userErrors { field message }
  }
}
"""


async def upload_image(file_bytes: bytes, filename: str) -> str:
    """Uploads one image to Shopify's staged-upload endpoint and returns the
    resourceUrl to reference it in productSet's `files`."""
    mime_type = mimetypes.guess_type(filename)[0] or "image/jpeg"

    data = await _graphql(
        _STAGED_UPLOAD_MUTATION,
        {
            "input": [
                {
                    "filename": filename,
                    "mimeType": mime_type,
                    "resource": "PRODUCT_IMAGE",
                    "httpMethod": "POST",
                }
            ]
        },
    )
    errors = data["stagedUploadsCreate"]["userErrors"]
    if errors:
        raise RuntimeError(f"Shopify staged upload error: {errors}")

    target = data["stagedUploadsCreate"]["stagedTargets"][0]
    form = {p["name"]: p["value"] for p in target["parameters"]}

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            target["url"],
            data=form,
            files={"file": (filename, file_bytes, mime_type)},
        )
        response.raise_for_status()

    return target["resourceUrl"]


_PRODUCT_SET_MUTATION = """
mutation productSet($input: ProductSetInput!, $synchronous: Boolean!) {
  productSet(input: $input, synchronous: $synchronous) {
    product {
      id
      title
      onlineStoreUrl
      variants(first: 20) {
        nodes { id title price sku inventoryQuantity }
      }
    }
    userErrors { field message code }
  }
}
"""


async def create_product(
    title: str,
    description_html: str,
    product_type: str,
    tags: list[str],
    variants: list[dict],
    image_resource_urls: list[str],
) -> dict:
    """Creates the product as a DRAFT (never auto-published — a human still
    has to publish it from Shopify admin) with one variant per size and
    starting inventory. Raises RuntimeError with Shopify's userErrors on
    failure; nothing partial is left behind since productSet is one call."""
    location_id = await get_primary_location_id()

    input_payload = {
        "title": title,
        "descriptionHtml": description_html,
        "status": "DRAFT",
        "productType": product_type,
        "tags": tags,
        "productOptions": [
            {
                "name": "Size",
                "position": 1,
                "values": [{"name": v["size"]} for v in variants],
            }
        ],
        "variants": [
            {
                "optionValues": [{"optionName": "Size", "name": v["size"]}],
                "price": v["price"],
                "sku": v["sku"],
                "inventoryQuantities": [
                    {
                        "locationId": location_id,
                        "name": "available",
                        "quantity": v["quantity"],
                    }
                ],
            }
            for v in variants
        ],
        "files": [
            {"originalSource": url, "contentType": "IMAGE"} for url in image_resource_urls
        ],
    }

    data = await _graphql(_PRODUCT_SET_MUTATION, {"input": input_payload, "synchronous": True})
    result = data["productSet"]
    if result["userErrors"]:
        raise RuntimeError(f"Shopify product creation error: {result['userErrors']}")
    return result["product"]
