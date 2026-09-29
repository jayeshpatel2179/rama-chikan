from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


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
    entry point."""
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
