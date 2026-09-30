import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import shopify_client
from bot.config import VALID_SIZES
from bot.handlers.cancel import cancel

logger = logging.getLogger(__name__)

WAITING_PRODUCT, WAITING_ACTION, WAITING_SIZES, WAITING_DELETE_CONFIRM = range(4)

# Matches either a full product URL ("…/products/some-handle") or a bare
# handle-looking string (hyphen-separated lowercase/number segments) sent on
# its own — distinct enough from the new-product flow's free-text answers
# that the two entry points don't collide.
#
# 2026-09-29 bug fix: the bare-handle alternative used to cap at 7 segments
# ({1,6} additional groups) — any real handle longer than that (a fairly
# short AI-generated title already produces 8-9 segments, and Shopify's
# auto-appended "-1"/"-2" duplicate-title suffix pushes shorter ones over
# too) silently failed to match, so the message never even reached this
# ConversationHandler; it fell through to the idle catch-all instead, with
# no error of any kind. Confirmed by testing the old pattern directly
# against real generated-title-shaped handles. The full-URL alternative was
# never affected (unbounded already) — only typing the bare slug alone hit
# this. Removed the upper cap ({1,6} -> {1,}) but kept the "at least one
# hyphen" floor (still {1,...}, not {0,...}) — that floor is what keeps a
# plain idle word like "hi" or "hello" from being misread as a product
# handle and falling into this flow instead of the idle Yes/No prompt.
PRODUCT_REF_FILTER = filters.Regex(
    r"(?i)(/products/[a-z0-9\-]+)|^[a-z0-9]+(-[a-z0-9]+){1,}$"
)

_ACTION_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("📦 Mark out of stock", callback_data="action_mark")],
        [InlineKeyboardButton("🗑️ Delete this product from store", callback_data="action_delete")],
        [InlineKeyboardButton("❌ Wrong product, try again", callback_data="action_wrong")],
    ]
)


async def receive_product_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """2026-09-30 routing fix: every text-consuming return in this function
    (and in receive_sizes_to_mark below) raises ApplicationHandlerStop(state)
    instead of plainly `return`ing the state. See that exception's own
    docstring — python-telegram-bot evaluates EVERY handler group for EVERY
    update by default; a plain `return` here only tells THIS
    ConversationHandler what to do next, it does nothing to stop
    bot.handlers.start.unrecognized_text (registered in the next group,
    group=1) from ALSO independently matching the exact same text update and
    firing right alongside it. That's what was actually causing a product
    slug/URL to appear to be "swallowed" by the 4-button menu — the delete
    flow was running correctly the whole time, the menu was just an
    unwanted EXTRA message stacked on top of it, worst right after a
    completed posting cycle (chat_data["draft"] is None and the 24h
    confirm-Yes gate is fresh at exactly that moment, so unrecognized_text's
    "already said Yes recently" branch fires instead of its milder Yes/No
    nudge). Raising ApplicationHandlerStop here both sets this
    ConversationHandler's next state AND stops every lower-priority group
    from touching this update at all, regardless of what state the
    out-of-stock conversation is currently in."""
    text = update.message.text.strip()
    try:
        product = await shopify_client.lookup_product(text)
    except Exception:
        logger.exception("Product lookup failed")
        product = None

    if product is None:
        await update.message.reply_text(
            "Couldn't find that product — send the product slug or the full "
            "product URL again."
        )
        raise ApplicationHandlerStop(WAITING_PRODUCT)

    context.chat_data["oos_product"] = product
    new_state = await _send_action_menu(update.message, product)
    raise ApplicationHandlerStop(new_state)


async def _send_action_menu(message, product: dict) -> int:
    image_url = None
    preview = product.get("featuredMedia", {}).get("preview") if product.get("featuredMedia") else None
    if preview and preview.get("image"):
        image_url = preview["image"]["url"]

    caption = f"Found it:\n\n*{product['title']}*\n\nWhat do you want to do?"
    if image_url:
        await message.reply_photo(image_url, caption=caption, reply_markup=_ACTION_KEYBOARD, parse_mode="Markdown")
    else:
        await message.reply_text(caption, reply_markup=_ACTION_KEYBOARD, parse_mode="Markdown")
    return WAITING_ACTION


async def action_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

    if query.data == "action_wrong":
        await query.message.reply_text("No problem — send the correct product slug or URL.")
        return WAITING_PRODUCT

    product = context.chat_data["oos_product"]

    if query.data == "action_delete":
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🗑️ Yes, delete it", callback_data="delete_yes")],
                [InlineKeyboardButton("↩️ No, go back", callback_data="delete_no")],
            ]
        )
        await query.message.reply_text(
            f"Permanently delete *{product['title']}* from the store? This can't be undone.",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return WAITING_DELETE_CONFIRM

    # action_mark
    sizes_in_product = sorted(
        {
            opt["value"]
            for v in product["variants"]["nodes"]
            for opt in v["selectedOptions"]
            if opt["name"] == "Size"
        },
        key=lambda s: VALID_SIZES.index(s) if s in VALID_SIZES else 99,
    )
    await query.message.reply_text(
        f"This product has sizes: {', '.join(sizes_in_product)}.\n\n"
        "Which size(s) should I mark out of stock? Reply with one or more "
        "sizes (e.g. \"M\" or \"M XL\"), or say \"mark whole product out of stock\"."
    )
    return WAITING_SIZES


async def delete_confirm_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

    product = context.chat_data["oos_product"]

    if query.data == "delete_no":
        return await _send_action_menu(query.message, product)

    try:
        await shopify_client.delete_product(product["id"])
    except Exception as exc:
        logger.exception("Failed to delete product")
        await query.message.reply_text(
            f"Couldn't delete *{product['title']}* — nothing was changed.\n"
            f"Reason: {exc}\n\nTry again, or send the product slug/URL again to retry.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await query.message.reply_text(
        f"🗑️ Deleted *{product['title']}* from the store.", parse_mode="Markdown"
    )
    context.chat_data.pop("oos_product", None)
    return ConversationHandler.END


async def receive_sizes_to_mark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """See receive_product_ref's docstring for why every text-consuming
    return below raises ApplicationHandlerStop(state) instead of a plain
    `return` — same routing fix, same reason, applied consistently to the
    rest of this conversation's text-answer states."""
    text = update.message.text.strip().lower()
    product = context.chat_data["oos_product"]
    variants = product["variants"]["nodes"]

    if "whole product" in text or "whole thing" in text or "entire product" in text:
        target_variants = variants
    else:
        requested = {tok.upper() for tok in text.replace(",", " ").split()}
        requested = {s for s in requested if s in VALID_SIZES}
        if not requested:
            await update.message.reply_text(
                'Didn\'t catch a valid size — reply with size(s) like "M" or "M XL", '
                'or say "mark whole product out of stock".'
            )
            raise ApplicationHandlerStop(WAITING_SIZES)

        target_variants = [
            v
            for v in variants
            if any(opt["name"] == "Size" and opt["value"] in requested for opt in v["selectedOptions"])
        ]
        if not target_variants:
            await update.message.reply_text(
                "None of those sizes exist on this product — check the size list above and resend."
            )
            raise ApplicationHandlerStop(WAITING_SIZES)

    inventory_items = [
        {"inventory_item_id": v["inventoryItem"]["id"], "current_quantity": v["inventoryQuantity"]}
        for v in target_variants
    ]

    try:
        await shopify_client.mark_variants_out_of_stock(inventory_items)
    except Exception as exc:
        logger.exception("Failed to mark variants out of stock")
        await update.message.reply_text(
            f"Couldn't update *{product['title']}* on Shopify — nothing was changed.\n"
            f"Reason: {exc}\n\nTry again, or send the product slug/URL again to retry.",
            parse_mode="Markdown",
        )
        raise ApplicationHandlerStop(ConversationHandler.END)

    changed_sizes = sorted(
        {
            opt["value"]
            for v in target_variants
            for opt in v["selectedOptions"]
            if opt["name"] == "Size"
        },
        key=lambda s: VALID_SIZES.index(s) if s in VALID_SIZES else 99,
    )
    await update.message.reply_text(
        f"✅ Marked out of stock on *{product['title']}*: {', '.join(changed_sizes)}",
        parse_mode="Markdown",
    )
    context.chat_data.pop("oos_product", None)
    raise ApplicationHandlerStop(ConversationHandler.END)


def build_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(PRODUCT_REF_FILTER & ~filters.COMMAND, receive_product_ref)],
        states={
            WAITING_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_product_ref)],
            WAITING_ACTION: [
                CallbackQueryHandler(action_tap, pattern="^action_(mark|delete|wrong)$")
            ],
            WAITING_DELETE_CONFIRM: [
                CallbackQueryHandler(delete_confirm_tap, pattern="^delete_(yes|no)$")
            ],
            WAITING_SIZES: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sizes_to_mark)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=900,
        # per_user=False: same reasoning as new_product.py's ConversationHandler
        # — one shared session per chat, not per Telegram user, and state
        # lives in context.chat_data. See that file's comment for why.
        per_user=False,
    )
