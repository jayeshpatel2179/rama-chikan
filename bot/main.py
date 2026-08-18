import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from bot.config import LOG_LEVEL, SHOPKEEPER_ALLOWED_USER_IDS, TELEGRAM_BOT_TOKEN
from bot.handlers.start import help_command, start

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=LOG_LEVEL
)
logger = logging.getLogger(__name__)


async def _whitelist_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Runs before every other handler (group=-1). Anything from a
    # non-whitelisted user id is dropped silently — no reply, just a log line
    # with the id so it can be added to SHOPKEEPER_ALLOWED_USER_IDS if it
    # should be let in.
    user = update.effective_user
    if user is None or user.id not in SHOPKEEPER_ALLOWED_USER_IDS:
        if user is not None:
            logger.warning(
                "Blocked message from unauthorized Telegram user id=%s username=%s",
                user.id,
                user.username,
            )
        raise ApplicationHandlerStop


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception while processing an update", exc_info=context.error)


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(TypeHandler(Update, _whitelist_gate), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_error_handler(_error_handler)

    return application


def main() -> None:
    application = build_application()
    logger.info("Starting Rama Chikan agent (polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
