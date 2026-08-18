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
