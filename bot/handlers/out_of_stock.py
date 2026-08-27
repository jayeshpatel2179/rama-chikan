import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
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
# handle-looking string (a few hyphen-separated lowercase/number segments)
# sent on its own — distinct enough from the new-product flow's free-text
# answers that the two entry points don't collide.
PRODUCT_REF_FILTER = filters.Regex(
    r"(?i)(/products/[a-z0-9\-]+)|^[a-z0-9]+(-[a-z0-9]+){1,6}$"
)

_ACTION_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("📦 Mark out of stock", callback_data="action_mark")],
        [InlineKeyboardButton("🗑️ Delete this product from store", callback_data="action_delete")],
        [InlineKeyboardButton("❌ Wrong product, try again", callback_data="action_wrong")],
    ]
)


async def receive_product_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        return WAITING_PRODUCT

    context.user_data["oos_product"] = product
    return await _send_action_menu(update.message, product)


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

    product = context.user_data["oos_product"]

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

    product = context.user_data["oos_product"]

    if query.data == "delete_no":
        return await _send_action_menu(query.message, product)

    try:
        await shopify_client.delete_product(product["id"])
    except Exception:
        logger.exception("Failed to delete product")
        await query.message.reply_text(
            "Something went wrong deleting this on Shopify — nothing was changed. Try again."
        )
        return ConversationHandler.END

    await query.message.reply_text(
        f"🗑️ Deleted *{product['title']}* from the store.", parse_mode="Markdown"
    )
    context.user_data.pop("oos_product", None)
    return ConversationHandler.END


async def receive_sizes_to_mark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    product = context.user_data["oos_product"]
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
            return WAITING_SIZES

        target_variants = [
            v
            for v in variants
            if any(opt["name"] == "Size" and opt["value"] in requested for opt in v["selectedOptions"])
        ]
        if not target_variants:
            await update.message.reply_text(
                "None of those sizes exist on this product — check the size list above and resend."
            )
            return WAITING_SIZES

    inventory_items = [
        {"inventory_item_id": v["inventoryItem"]["id"], "current_quantity": v["inventoryQuantity"]}
        for v in target_variants
    ]

    try:
        await shopify_client.mark_variants_out_of_stock(inventory_items)
    except Exception:
        logger.exception("Failed to mark variants out of stock")
        await update.message.reply_text(
            "Something went wrong updating Shopify — nothing was changed. Try again."
        )
        return ConversationHandler.END

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
    context.user_data.pop("oos_product", None)
    return ConversationHandler.END


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
    )
