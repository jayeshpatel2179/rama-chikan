from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler


def _who(update: Update) -> str:
    user = update.effective_user
    if user and user.full_name:
        return user.full_name
    chat = update.effective_chat
    return chat.title if chat and chat.title else "someone"


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Global /cancel fallback, shared by both conversation flows. This is a
    real kill switch, not just a reply:
      - cancels whatever async task is actively running for the session
        right now (mid image-gen, mid description-gen, mid photo-batch
        debounce -- see draft["active_task"], set by the handlers that do
        that work)
      - wipes chat_data completely: uploaded photos, intake answers,
        generated images, description, selected poses, locked background
        preset -- everything
      - any later tap on an already-rendered draft's buttons checks its
        embedded session_id against chat_data and finds it gone, so it
        replies "This draft was cancelled" instead of silently doing
        nothing or crashing (see new_product.py's _require_live_draft)

    Session state lives in context.chat_data (NOT user_data), and both
    ConversationHandlers are per_chat=True, per_user=False -- so this is one
    global session per chat, not one per member. That's required for two
    real reasons: (a) the bot runs in a shared group with several members
    who must all be able to hit this, and (b) Telegram's "send anonymously"
    group-admin option makes effective_user a different pseudo-identity per
    message, so keying anything on per_user silently breaks even the
    original sender's own /cancel.
    """
    draft = context.chat_data.get("draft")
    if draft is None:
        await update.effective_message.reply_text("Nothing in progress right now.")
        return ConversationHandler.END

    task = draft.get("active_task")
    if task is not None and not task.done():
        task.cancel()

    context.chat_data.clear()
    await update.effective_message.reply_text(
        f"Session cancelled by {_who(update)}. All progress cleared — send new photos to start again."
    )
    return ConversationHandler.END


async def cancel_idle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level /cancel — only reached when no conversation is active for this chat."""
    await update.effective_message.reply_text("Nothing in progress right now.")
