from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Shared /cancel fallback for both conversation flows — wipes whatever
    draft/lookup was in progress and exits cleanly. Nothing is published or
    changed on Shopify by cancelling."""
    context.user_data.clear()
    await update.effective_message.reply_text("Cancelled — nothing was changed.")
    return ConversationHandler.END


async def cancel_idle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level /cancel — only reached when no conversation is active."""
    await update.effective_message.reply_text("Nothing in progress right now.")
