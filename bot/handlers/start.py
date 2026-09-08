from telegram import Update
from telegram.ext import ContextTypes


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Rama Chikan store agent is online.\n\n"
        "• New product: send the raw garment photo(s) (1 is fine, or a few angles).\n"
        "• Out of stock: send the product slug or product URL."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "New product: send raw photo(s) of the garment (front is enough, more "
        "angles help), tap \"That's all the photos\" when done, then answer "
        "the 5 questions I ask in one message.\n\n"
        "Out of stock: send the product slug or full product URL, confirm "
        "it's the right one, then tell me which size(s) — or say "
        '"mark whole product out of stock".'
    )


async def unrecognized_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registered in a later handler group (see bot/main.py) so it only ever
    fires when nothing else claimed the update — i.e. no session is active
    for this chat and the text isn't a product slug/URL either. Covers
    plain messages like "hi" that would otherwise get silently dropped."""
    await update.effective_message.reply_text(
        "Send the product photo(s) to start a new listing, or the product "
        "slug/URL to manage stock. /help for details."
    )
