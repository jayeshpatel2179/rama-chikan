import os

from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _parse_user_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    return frozenset(int(part.strip()) for part in raw.split(",") if part.strip())


TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")

# Empty means nobody is allowed in yet — safe default until the shopkeeper's
# own Telegram user id is added here. See bot/main.py's whitelist gate: an
# unauthorized message gets logged with the sender's id so it can be copied
# into this env var on first contact.
SHOPKEEPER_ALLOWED_USER_IDS = _parse_user_ids(os.getenv("SHOPKEEPER_ALLOWED_USER_IDS"))

LOG_LEVEL = "INFO"
