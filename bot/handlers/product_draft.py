import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import ai, shopify_client

logger = logging.getLogger(__name__)

WAITING_PHOTOS, WAITING_FABRIC, WAITING_SIZES, WAITING_PRICE, CONFIRMING = range(5)

MAX_PHOTOS = 4
_SIZE_PAIR_RE = re.compile(r"([A-Za-z0-9]+)\s*[:\-]\s*(\d+)")


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photos = context.user_data.setdefault("photos", [])
    largest = update.message.photo[-1]
    photos.append(largest.file_id)

    if len(photos) >= MAX_PHOTOS:
        return await _finish_photos(update, context)

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("That's all the photos", callback_data="photos_done")]]
    )
    await update.message.reply_text(
        f"Got {len(photos)}/{MAX_PHOTOS} photo(s). Send more (front, back, fabric "
        "close-up, detail close-up), or tap below once you're done.",
        reply_markup=keyboard,
    )
    return WAITING_PHOTOS


async def photos_done_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    return await _finish_photos(update, context)


async def _finish_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photos = context.user_data.get("photos", [])
    message = update.effective_message
    if not photos:
        await message.reply_text("Send at least one photo first.")
        return WAITING_PHOTOS

    await message.reply_text("Looking at the photos...")

    bot = context.bot
    photo_bytes_list = []
    for file_id in photos:
        file = await bot.get_file(file_id)
        photo_bytes_list.append(bytes(await file.download_as_bytearray()))
    context.user_data["photo_bytes"] = photo_bytes_list

    analysis = await ai.analyze_photos(photo_bytes_list)
    context.user_data["draft"] = {
        "garment_type": analysis["garment_type"],
        "dominant_color": analysis["dominant_color"],
        "embroidery_description": analysis["embroidery_or_pattern_description"],
    }

    await message.reply_text(
        f"Looks like a {analysis['dominant_color']} {analysis['garment_type']} — "
        f"{analysis['embroidery_or_pattern_description']}\n\n"
        "What fabric is this? (e.g. Cotton, Rayon, Georgette)"
    )
    return WAITING_FABRIC


async def receive_fabric(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["draft"]["fabric"] = update.message.text.strip()
    await update.message.reply_text('Sizes and how many of each? e.g. "S:4 M:6 L:5 XL:3"')
    return WAITING_SIZES


async def receive_sizes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    pairs = _SIZE_PAIR_RE.findall(text)
    if not pairs:
        await update.message.reply_text(
            'Didn\'t catch that — please use the format "S:4 M:6 L:5".'
        )
        return WAITING_SIZES

    context.user_data["draft"]["sizes"] = [
        {"size": size.upper(), "quantity": int(qty)} for size, qty in pairs
    ]
    await update.message.reply_text("Price for this product?")
    return WAITING_PRICE


async def receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", "").replace("₹", "")
    try:
        price = float(text)
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a valid positive price, e.g. 2499")
        return WAITING_PRICE

    draft = context.user_data["draft"]
    draft["price"] = price

    await update.message.reply_text("Writing the description...")
    copy = await ai.generate_description(
        draft["garment_type"],
        draft["fabric"],
        draft["dominant_color"],
        draft["embroidery_description"],
    )
    draft["title"] = copy["title"]
    draft["description_html"] = copy["description_html"]

    sizes_summary = ", ".join(f"{s['size']}: {s['quantity']}" for s in draft["sizes"])
    preview = (
        f"*{draft['title']}*\n\n"
        f"{draft['description_html']}\n\n"
        f"Fabric: {draft['fabric']}\n"
        f"Sizes: {sizes_summary}\n"
        f"Price: ₹{draft['price']:.2f}\n\n"
        "This will be created as a DRAFT on Shopify — not visible to customers "
        "until you publish it there. Confirm?"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Confirm", callback_data="confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]
    )
    await update.message.reply_text(preview, reply_markup=keyboard, parse_mode="Markdown")
    return CONFIRMING


async def confirm_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Creating the product on Shopify...")

    draft = context.user_data["draft"]
    photo_bytes_list = context.user_data.get("photo_bytes", [])

    try:
        image_urls = []
        for i, photo_bytes in enumerate(photo_bytes_list):
            url = await shopify_client.upload_image(photo_bytes, f"product-{i}.jpg")
            image_urls.append(url)

        sku_base = re.sub(r"[^A-Za-z0-9]+", "-", draft["title"]).strip("-").upper()[:20]
        variants = [
            {
                "size": s["size"],
                "price": f"{draft['price']:.2f}",
                "sku": f"{sku_base}-{s['size']}",
                "quantity": s["quantity"],
            }
            for s in draft["sizes"]
        ]

        product = await shopify_client.create_product(
            title=draft["title"],
            description_html=draft["description_html"],
            product_type=draft["garment_type"],
            tags=[draft["fabric"], draft["dominant_color"]],
            variants=variants,
            image_resource_urls=image_urls,
        )
    except Exception:
        logger.exception("Failed to create product on Shopify")
        await query.message.reply_text(
            "Something went wrong creating this on Shopify — nothing was published. "
            "Check the logs, then try again."
        )
        context.user_data.clear()
        return ConversationHandler.END

    await query.message.reply_text(
        f"✅ Product created as a draft: *{product['title']}*\n"
        f"Product ID: `{product['id']}`\n\n"
        "Review it and publish from Shopify admin when you're ready for it to go live.",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Cancelled — nothing was created.")
    context.user_data.clear()
    return ConversationHandler.END


def build_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, receive_photo)],
        states={
            WAITING_PHOTOS: [
                MessageHandler(filters.PHOTO, receive_photo),
                CallbackQueryHandler(photos_done_tap, pattern="^photos_done$"),
            ],
            WAITING_FABRIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_fabric)],
            WAITING_SIZES: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sizes)],
            WAITING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_price)],
            CONFIRMING: [
                CallbackQueryHandler(confirm_tap, pattern="^confirm$"),
                CallbackQueryHandler(cancel_tap, pattern="^cancel$"),
            ],
        },
        fallbacks=[],
        conversation_timeout=600,
    )
