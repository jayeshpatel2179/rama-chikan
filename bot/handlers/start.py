from telegram import Update
from telegram.ext import ContextTypes


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Rama Chikan shopkeeper agent is online.\n\n"
        "Send a product photo to add a new item, or just tell me what you need."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Send a product photo to start adding it.\n"
        "Or try things like:\n"
        "• \"set stock to 20\"\n"
        "• \"mark this out of stock\"\n"
        "• \"show today's orders\""
    )
