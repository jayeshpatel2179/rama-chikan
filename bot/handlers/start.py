from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot import state
from bot.handlers.new_product import _flow_choice_keyboard


def _start_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes ✅", callback_data="start_confirm_yes"),
                InlineKeyboardButton("No ❌", callback_data="start_confirm_no"),
            ]
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Rama Chikan store agent is online.\n\n"
        "• New product: /newproduct, then pick Kurti, Kurti + Pyjama Set, "
        "Dupatta, or Women Bottoms.\n"
        "• Out of stock: send the product slug or product URL."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "New product: /newproduct, then tap Kurti, Kurti + Pyjama Set, "
        "Dupatta, or Women Bottoms. I'll ask for the right photo(s) for "
        "that category, then send you its question set to answer in one "
        "message.\n\n"
        "Out of stock: send the product slug or full product URL, confirm "
        "it's the right one, then tell me which size(s) — or say "
        '"mark whole product out of stock".'
    )


async def unrecognized_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registered in a later handler group (see bot/main.py) so it only ever
    fires when nothing else claimed the update — i.e. no session is active
    for this chat and the text isn't a product slug/URL either. Covers
    plain messages like "hi" or random text that would otherwise get
    silently dropped — offers to start a new listing (2026-09-29) instead
    of just pointing at /newproduct, so any idle message doubles as an
    entry point.

    2026-09-29: if this chat answered "Yes" within the last 24 hours
    (persisted — bot/state.py, survives a restart), skips the confirm
    question entirely and goes straight to the 4 category buttons — the
    SAME message start_new_product/confirm_yes_tap send, so tapping one
    works identically (those 4 buttons are registered as entry points on
    bot.handlers.new_product's ConversationHandler).

    2026-09-29 bug fix: a product still waiting on its GO LIVE buttons
    (CONFIRMING state) has no text handler registered at all, so any stray
    text the owner sends while it's sitting there (a stray word, an
    accidental message — anything that isn't a button tap) falls through
    every other handler and lands here too. Offering to start a FRESH
    product on top of one still pending is exactly the confusing "why is
    it asking me this already" case that was reported — so a draft still
    in flight (chat_data still has one — cleared only on finalize, abort,
    or /cancel) short-circuits straight to a reminder instead, skipping
    both the confirm question and the 24h skip-ahead above."""
    if context.chat_data.get("draft") is not None:
        await update.effective_message.reply_text(
            "You still have a product waiting on the buttons above — tap a "
            "GO LIVE button, ABORT, or /cancel before starting a new one."
        )
        return

    chat_id = update.effective_chat.id
    if state.confirm_yes_is_fresh(chat_id):
        context.chat_data.clear()
        await update.effective_message.reply_text(
            "What are you listing?", reply_markup=_flow_choice_keyboard()
        )
        return

    await update.effective_message.reply_text(
        "Do you want to push a product to Rama Chikan Store and its socials?",
        reply_markup=_start_confirm_keyboard(),
    )


async def confirm_no_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """"No" on the idle confirm prompt above — nothing was started, so
    there's no session to end; just acknowledges and clears the buttons."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Okay — send a message whenever you're ready.")
