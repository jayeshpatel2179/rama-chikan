import logging
import warnings

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.warnings import PTBUserWarning

from bot.config import LOG_LEVEL, TELEGRAM_BOT_TOKEN
from bot.handlers.cancel import cancel_idle
from bot.handlers.new_product import build_conversation_handler as build_new_product_handler
from bot.handlers.out_of_stock import build_conversation_handler as build_out_of_stock_handler
from bot.handlers.start import help_command, start

# Both conversation flows intentionally mix message handlers (photos/text)
# and callback-query handlers (button taps) in the same ConversationHandler
# — a supported combination PTB warns about by default because it means
# callback queries aren't tracked with per-message precision. That precision
# isn't needed here: this bot only ever tracks one conversation per owner at
# a time, so the warning is a known false positive for this design.
warnings.filterwarnings(
    "ignore", message=r"If 'per_message=False'.*", category=PTBUserWarning
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=LOG_LEVEL
)
logger = logging.getLogger(__name__)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception while processing an update", exc_info=context.error)


def build_application() -> Application:
    # concurrent_updates lets a photo's handler still be "awake" (mid debounce
    # sleep in new_product.receive_photo) when the next photo of the same
    # multi-select batch arrives, so the batch can be detected and announced
    # once instead of once per photo. Safe for this single-owner bot.
    #
    # PTB's default network timeouts (5s connect/read/write) are too short
    # for uploading the generated product photo(s) back to Telegram as a
    # media group. Photo/file uploads (sendMediaGroup, sendPhoto) actually
    # use a SEPARATE `media_write_timeout` (PTB default: 20s), not the
    # general `write_timeout` below — that's the one that was still timing
    # out. Both are set generously here now.
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(60)
        .read_timeout(60)
        .write_timeout(60)
        .media_write_timeout(120)
        .pool_timeout(60)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(build_new_product_handler())
    application.add_handler(build_out_of_stock_handler())
    # Registered after both conversation handlers so an in-progress
    # conversation's own /cancel fallback takes it first; this one only
    # fires when nothing is active.
    application.add_handler(CommandHandler("cancel", cancel_idle))
    application.add_error_handler(_error_handler)

    return application


def main() -> None:
    application = build_application()
    logger.info("Starting Rama Chikan agent (polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
