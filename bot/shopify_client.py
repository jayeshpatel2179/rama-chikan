import logging
import mimetypes
import time
import uuid

import httpx

from bot.config import (
    BESTSELLERS_COLLECTION_ID,
    CATEGORY_COLLECTIONS,
    FRONTPAGE_COLLECTION_ID,
    KURTAS_COLLECTION_ID,
    MATERIAL_METAFIELD_CHOICES,
    NEW_ARRIVALS_COLLECTION_ID,
    ONLINE_STORE_PUBLICATION_ID,
    PRODUCT_CATEGORY_METAFIELD_VALUE,
    SHOPIFY_CLIENT_ID,
    SHOPIFY_CLIENT_SECRET,
    SHOPIFY_STORE_DOMAIN,
    VALID_SIZES,
)

logger = logging.getLogger(__name__)

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
    mime_type = mimetypes.guess_type(filename)[0] or "image/png"

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


def compute_compare_at_price(price: float, discount_pct: float) -> float:
    """Back-calculates the "original" price from the real selling price and a
    display discount percentage, so the storefront shows the struck-through
    original beside the real price (e.g. price 1500 at 23% off -> ~1950)."""
    if discount_pct <= 0:
        return None
    return round(price / (1 - discount_pct / 100))


def resolve_material_metafield(material: str) -> str | None:
    normalized = material.strip().lower()
    for choice in MATERIAL_METAFIELD_CHOICES:
        if choice.lower() == normalized:
            return choice
    return None


_PRODUCT_SET_MUTATION = """
mutation productSet($input: ProductSetInput!, $synchronous: Boolean!) {
  productSet(input: $input, synchronous: $synchronous) {
    product {
      id
      title
      handle
      onlineStoreUrl
      featuredImage { url }
      variants(first: 20) {
        nodes { id title price sku inventoryQuantity }
      }
    }
    userErrors { field message code }
  }
}
"""


async def create_live_product(
    title: str,
    description_html: str,
    tags: list[str],
    price: float,
    compare_at_price: float | None,
    size_quantities: dict[str, int],
    image_resource_urls: list[str],
    material: str,
    categories: list[str],
    is_bestseller: bool,
) -> dict:
    """Creates the product LIVE (status ACTIVE, published to Online Store) —
    this is only called after the owner taps GO LIVE. Every one of the
    store's standard sizes is created as a variant; sizes not mentioned by
    the owner get quantity 0 and inventoryPolicy DENY so they render as
    "Sold out" and cannot be purchased, rather than being omitted."""
    location_id = await get_primary_location_id()

    variants = []
    for size in VALID_SIZES:
        quantity = size_quantities.get(size, 0)
        variant = {
            "optionValues": [{"optionName": "Size", "name": size}],
            "price": f"{price:.2f}",
            "sku": f"{title[:20].strip().replace(' ', '-').upper()}-{size}",
            "inventoryPolicy": "DENY",
            "inventoryQuantities": [
                {"locationId": location_id, "name": "available", "quantity": quantity}
            ],
        }
        if compare_at_price:
            variant["compareAtPrice"] = f"{compare_at_price:.2f}"
        variants.append(variant)

    metafields = [
        {
            "namespace": "custom",
            "key": "category",
            "type": "single_line_text_field",
            "value": PRODUCT_CATEGORY_METAFIELD_VALUE,
        }
    ]
    resolved_material = resolve_material_metafield(material)
    if resolved_material:
        metafields.append(
            {
                "namespace": "custom",
                "key": "material",
                "type": "single_line_text_field",
                "value": resolved_material,
            }
        )

    input_payload = {
        "title": title,
        "descriptionHtml": description_html,
        "status": "ACTIVE",
        "productType": "Kurta Set",
        "tags": tags,
        "productOptions": [
            {"name": "Size", "position": 1, "values": [{"name": s} for s in VALID_SIZES]}
        ],
        "variants": variants,
        "files": [
            {"originalSource": url, "contentType": "IMAGE"} for url in image_resource_urls
        ],
        "metafields": metafields,
    }

    data = await _graphql(_PRODUCT_SET_MUTATION, {"input": input_payload, "synchronous": True})
    result = data["productSet"]
    if result["userErrors"]:
        raise RuntimeError(f"Shopify product creation error: {result['userErrors']}")
    product = result["product"]

    # Every live product goes into Kurtas (main nav) plus the 2 homepage
    # "curated" collections (New Arrivals, the Home page grid) — those two
    # are manually-sorted on Shopify's side, so nothing shows there unless
    # explicitly added here. Bestsellers only gets products the owner
    # actually flagged as a bestseller.
    collection_ids = [KURTAS_COLLECTION_ID, NEW_ARRIVALS_COLLECTION_ID, FRONTPAGE_COLLECTION_ID]
    tile_collection_ids = [KURTAS_COLLECTION_ID]
    for category in categories:
        category_key = category.strip().lower()
        collection_id = CATEGORY_COLLECTIONS.get(category_key)
        if collection_id:
            collection_ids.append(collection_id)
            tile_collection_ids.append(collection_id)
    if is_bestseller:
        collection_ids.append(BESTSELLERS_COLLECTION_ID)

    await _add_to_collections(product["id"], collection_ids)
    await _publish_to_online_store(product["id"])

    image_url = (product.get("featuredImage") or {}).get("url")
    if image_url:
        try:
            await _update_collection_images(tile_collection_ids, image_url)
        except Exception:
            # Cosmetic only (keeps "Shop by Category" homepage tiles showing
            # the newest item) — must never fail an already-live product.
            logger.exception("Failed to refresh collection tile images")

    return product


_COLLECTION_ADD_MUTATION = """
mutation collectionAddProducts($id: ID!, $productIds: [ID!]!) {
  collectionAddProducts(id: $id, productIds: $productIds) {
    collection { title }
    userErrors { field message }
  }
}
"""


async def _add_to_collections(product_id: str, collection_ids: list[str]) -> None:
    for collection_id in collection_ids:
        data = await _graphql(
            _COLLECTION_ADD_MUTATION, {"id": collection_id, "productIds": [product_id]}
        )
        errors = data["collectionAddProducts"]["userErrors"]
        if errors:
            raise RuntimeError(f"Shopify collection add error: {errors}")


_COLLECTION_UPDATE_MUTATION = """
mutation collectionUpdate($input: CollectionInput!) {
  collectionUpdate(input: $input) {
    collection { id }
    userErrors { field message }
  }
}
"""


async def _update_collection_images(collection_ids: list[str], image_url: str) -> None:
    """Shopify collections don't derive their homepage card image from member
    products automatically — a collection.image has to be set explicitly, so
    without this the "Shop by Category" tiles stay on the generic illustrated
    placeholder forever. Called with the just-created product's own image, so
    each touched category's tile always shows its most recently pushed item."""
    for collection_id in collection_ids:
        data = await _graphql(
            _COLLECTION_UPDATE_MUTATION,
            {"input": {"id": collection_id, "image": {"src": image_url}}},
        )
        errors = data["collectionUpdate"]["userErrors"]
        if errors:
            raise RuntimeError(f"Shopify collection image update error: {errors}")


_PUBLISH_MUTATION = """
mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    userErrors { field message }
  }
}
"""


async def _publish_to_online_store(product_id: str) -> None:
    data = await _graphql(
        _PUBLISH_MUTATION,
        {"id": product_id, "input": [{"publicationId": ONLINE_STORE_PUBLICATION_ID}]},
    )
    errors = data["publishablePublish"]["userErrors"]
    if errors:
        raise RuntimeError(f"Shopify publish error: {errors}")


_PRODUCT_LOOKUP_QUERY = """
query productByHandle($handle: String!) {
  productByHandle(handle: $handle) {
    id
    title
    handle
    featuredMedia {
      preview {
        image { url }
      }
    }
    variants(first: 20) {
      nodes {
        id
        selectedOptions { name value }
        inventoryItem { id }
        inventoryQuantity
      }
    }
  }
}
"""


def extract_handle(slug_or_url: str) -> str:
    text = slug_or_url.strip()
    if "/products/" in text:
        text = text.split("/products/", 1)[1]
    return text.split("?", 1)[0].strip("/ ")


async def lookup_product(slug_or_url: str) -> dict | None:
    handle = extract_handle(slug_or_url)
    data = await _graphql(_PRODUCT_LOOKUP_QUERY, {"handle": handle})
    return data["productByHandle"]


_INVENTORY_SET_MUTATION = """
mutation inventorySetQuantities($input: InventorySetQuantitiesInput!, $idempotencyKey: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
    userErrors { field message }
  }
}
"""


async def mark_variants_out_of_stock(inventory_items: list[dict]) -> None:
    """`inventory_items` is a list of {"inventory_item_id", "current_quantity"}
    — Shopify's inventorySetQuantities requires changeFromQuantity (the
    quantity you're changing FROM) as an optimistic-concurrency check, and
    (as of the 2026-04 API changes) an @idempotent directive with a unique
    key on every call — neither is optional in this API version."""
    location_id = await get_primary_location_id()
    quantities = [
        {
            "inventoryItemId": item["inventory_item_id"],
            "locationId": location_id,
            "quantity": 0,
            "changeFromQuantity": item["current_quantity"],
        }
        for item in inventory_items
    ]
    data = await _graphql(
        _INVENTORY_SET_MUTATION,
        {
            "idempotencyKey": str(uuid.uuid4()),
            "input": {
                "name": "available",
                "reason": "correction",
                "quantities": quantities,
            }
        },
    )
    errors = data["inventorySetQuantities"]["userErrors"]
    if errors:
        raise RuntimeError(f"Shopify inventory update error: {errors}")


_PRODUCT_DELETE_MUTATION = """
mutation productDelete($input: ProductDeleteInput!) {
  productDelete(input: $input) {
    deletedProductId
    userErrors { field message }
  }
}
"""


async def delete_product(product_id: str) -> None:
    data = await _graphql(_PRODUCT_DELETE_MUTATION, {"input": {"id": product_id}})
    errors = data["productDelete"]["userErrors"]
    if errors:
        raise RuntimeError(f"Shopify product delete error: {errors}")
