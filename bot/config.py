import os

from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = _require_env("OPENAI_API_KEY")
SHOPIFY_STORE_DOMAIN = _require_env("SHOPIFY_STORE_DOMAIN")
SHOPIFY_CLIENT_ID = _require_env("SHOPIFY_CLIENT_ID")
SHOPIFY_CLIENT_SECRET = _require_env("SHOPIFY_CLIENT_SECRET")

LOG_LEVEL = "INFO"

# --- Instagram publishing (Part 5, 2026-09-16) -----------------------------
# Upload-post credentials — not required at startup (mirrors the pattern in
# the sibling ped-instacap-poster / pedtalks-insta-image bots, which this
# integration is modeled on). Starts pointed at the SAME dummy test profile
# those bots use, copied into .env directly rather than typed here — switch
# to the live Rama Chikan profile later by changing only INSTAGRAM_PROFILE_NAME
# in .env, no code edit needed.
UPLOAD_POST_API_KEY = os.getenv("UPLOAD_POST_API_KEY")
INSTAGRAM_PROFILE_NAME = os.getenv("INSTAGRAM_PROFILE_NAME")

# --- Shopify catalog constants -------------------------------------------

# Every product from Flow 1 is added to the store's main Kurtis collection
# (the primary storefront nav tab, renamed from "Kurtas" on the storefront
# side — the collection ID itself hasn't changed) plus every discovery tab
# the owner picks below (a product can belong to more than one). Kurtis
# stays unconditionally added to every product regardless of what Question 5
# selects, same as before this was expanded to 7 categories — every product
# this bot creates is a kurti, so removing that would silently empty the
# main nav tab for anything the owner doesn't also explicitly tick.
KURTAS_COLLECTION_ID = "gid://shopify/Collection/570532167991"

# The 2 new tabs added by the 2026-09 nav restructure, plus On Sale (which
# existed on the storefront already but was never wired into the bot's
# collection_ids join list until now — see the automatic-On-Sale logic in
# create_live_product below).
PREMIUM_COLLECTION_ID = "gid://shopify/Collection/572957360439"
KURTI_SETS_COLLECTION_ID = "gid://shopify/Collection/572957393207"
ON_SALE_COLLECTION_ID = "gid://shopify/Collection/570532299063"

# Keyed by the lowercased Question 5 category label. "for nani" and "kurtas"
# are kept as aliases so an owner who types the old wording still resolves
# to the right (renamed) collection — bot/handlers/new_product.py also
# normalizes the display label itself so the draft caption always shows the
# current name regardless of which wording the owner typed.
CATEGORY_COLLECTIONS = {
    "premium": PREMIUM_COLLECTION_ID,
    "kurtis": KURTAS_COLLECTION_ID,
    "kurtas": KURTAS_COLLECTION_ID,
    "kurti sets": KURTI_SETS_COLLECTION_ID,
    "kurti + pyjama set": KURTI_SETS_COLLECTION_ID,
    "for nani/dadi": "gid://shopify/Collection/570532200759",
    "for nani": "gid://shopify/Collection/570532200759",
    "for mom": "gid://shopify/Collection/570532233527",
    "for me": "gid://shopify/Collection/570532266295",
    "on sale": ON_SALE_COLLECTION_ID,
}

# These 3 homepage sections (Bestsellers carousel, New Arrivals carousel,
# "Shop the Full Collection" grid) all read from manually-curated Shopify
# collections — Shopify never adds products to them automatically, so every
# live product must be explicitly added via the API or these sections stay
# empty (and Shopify shows generic sample placeholder cards instead).
NEW_ARRIVALS_COLLECTION_ID = "gid://shopify/Collection/570532331831"
BESTSELLERS_COLLECTION_ID = "gid://shopify/Collection/570532364599"
FRONTPAGE_COLLECTION_ID = "gid://shopify/Collection/570052280631"

# Online Store sales channel publication — a product must be explicitly
# published here (in addition to status ACTIVE) to actually be visible to
# customers, not just exist in the Admin.
ONLINE_STORE_PUBLICATION_ID = "gid://shopify/Publication/312225268023"

VALID_SIZES = ["XS", "S", "M", "L", "XL", "XXL", "3XL"]

# custom.category is a metaobject-backed metafield with a fixed choice list
# on this store — "Kurta Set" is the closest valid value to what Rama Chikan
# actually sells (kurti + pyjama sets). Discovered by hitting INVALID_METAFIELD
# on free-text values during earlier catalog work.
PRODUCT_CATEGORY_METAFIELD_VALUE = "Kurta Set"

# custom.material is ALSO a fixed-choice metafield on this store. The
# owner's real materials (rayon, georgette, chikankari work) don't all land
# in this list, so it's only set when the owner's answer matches one of
# these (case-insensitive) — otherwise skipped and the raw material text
# still goes into tags + the description, just not this specific metafield.
MATERIAL_METAFIELD_CHOICES = [
    "Chanderi", "Cotton", "Cotton Cambric", "Cotton Crush Crepe",
    "Cotton Flex", "Cotton Loom", "Cotton Slub", "Glazed Cotton", "Kota",
    "Modal", "Mulmul", "Muslin", "Rayon", "Semi Chanderi", "Silk",
    "Viscose Muslin", "Wool",
]

# Reference photos for the 8 hardcoded premium editorial poses (bot/prompts.py
# PREMIUM_POSES) — used only for verifying the written pose descriptions
# match the supplied reference photos, never passed to the image model as
# generation references (same convention as references/poses/ for the
# standard 11-pose library).
PREMIUM_POSE_REFERENCE_DIR = r"C:\Agents\rama-chikan\Premium Poses"

# --- Image generation -----------------------------------------------------

IMAGE_GEN_MODEL = "gpt-image-2"
# Closest valid (edge % 16 == 0) size to the required 1000x1250 (4:5)
# output — cropped down to the exact 1000x1250 locally after generation.
IMAGE_GEN_SIZE = "1008x1264"
IMAGE_GEN_QUALITY = "medium"
FINAL_IMAGE_WIDTH = 1000
FINAL_IMAGE_HEIGHT = 1250

# How many of the owner's requested/auto-selected poses (Question 9,
# bot/prompts.py's 11-pose library) actually get generated right now.
# Raised to 11 (2026-08-27) to test the full pose system on a real product.
IMAGE_GENERATION_CAP = 11
