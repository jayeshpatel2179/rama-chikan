import asyncio
import html as html_lib
import io
import logging
import re
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import ai, image_gen, prompts, shopify_client
from bot.config import VALID_SIZES
from bot.handlers.cancel import cancel

logger = logging.getLogger(__name__)

WAITING_PHOTOS, WAITING_ANSWERS, CONFIRMING = range(3)

# No fixed count required — 1 raw photo (just the front) works fine, so does
# a handful of angles/close-ups. This is just a sane upper bound so nobody
# accidentally floods the draft with dozens of photos; output count is
# controlled separately by Question 9 (pose selection) + IMAGE_GENERATION_CAP,
# not by how many raw photos went in.
MAX_RAW_PHOTOS = 6

# There's no per-photo angle tagging in the intake flow — the owner just
# sends 1-6 undifferentiated raw photos. Poses 10/11 (back view) normally
# need a back-side reference (bot/prompts.py POSES), and this codebase has
# no reliable way to know if one of the raw photos is actually the back.
# Hardcoded False rather than guessed, per the "never invent" safety rule.
# NOTE: this only gates the bot's own AUTO pose picks (Question 9 answered
# with just a count) — when the owner explicitly names pose numbers,
# resolve_pose_selection() trusts them and does not apply this gate at all.
_HAS_BACK_REFERENCE = False

# Short menu labels for the chat — distinct from POSES[n].label (which is
# the fuller name used in the mega prompt) so the intake message stays scannable.
_POSE_MENU_LABELS = {
    1: "Front full-length",
    2: "Front waist-up",
    3: "Embroidery close-up",
    4: "Bottom only",
    5: "Side three-quarter full-length",
    6: "Hem & footwear close-up",
    7: "Three-quarter, looking down",
    8: "Waist-up, soft downward gaze",
    9: "Waist-up, looking to the side",
    10: "Back full-length",
    11: "Back over-the-shoulder",
}
assert set(_POSE_MENU_LABELS) == set(prompts.POSES), "pose menu is out of sync with bot.prompts.POSES"
_POSE_MENU = "\n".join(f"{n} = {label}" for n, label in _POSE_MENU_LABELS.items())

# How long to wait after the last photo before assuming the batch is done.
# When a phone sends several photos at once (multi-select), Telegram
# delivers them as separate messages a fraction of a second apart — this
# window lets them all land before the bot reacts, so a multi-photo send is
# announced once ("Received 2 photo(s)...") instead of once per photo.
_PHOTO_BATCH_DEBOUNCE_SECONDS = 2.0

_QUESTIONS_MESSAGE = (
    "A few quick questions — reply to all of them in ONE message:\n\n"
    "1. Material type (e.g. rayon, georgette, chikankari work)\n"
    "2. Sizes with quantity — e.g. \"3 of XS / 1 of S\". Sizes you don't mention "
    "will show on the site as out of stock.\n"
    "3. Price (the real selling price, e.g. 1500)\n"
    "4. Discount % to display (e.g. 20%, or say \"none\")\n"
    "5. Category — For Nani, For Mom, For Me (name more than one if it fits, "
    "or say \"all three\")\n"
    "6. Is this a best-selling kurti? (yes/no)\n"
    "7. Kurti length — short or long?\n"
    "8. What's in this listing — kurti + pyjama set, or kurti only?\n"
    "9. How many images, and which poses? Reply with pose numbers, e.g. "
    "\"1, 5, 3\" — or type \"all poses\" for all 11 — or just give a number "
    "like \"4\" and I'll pick the best combination.\n" + _POSE_MENU
)

_ALL_POSES_RE = re.compile(r"\ball\s+poses\b", re.I)

# The ready-to-post draft (generated images + description, sitting in memory
# waiting for a GO LIVE tap) expires 10 minutes after it's generated. GO LIVE
# and Regenerate Description both stop working past this and tell the owner
# it expired; ABORT always keeps working regardless, so an expired draft can
# still be cleared out without waiting for a fresh /cancel.
DRAFT_TTL_SECONDS = 10 * 60


def _new_session_id() -> str:
    return uuid.uuid4().hex[:8]


def _draft_expired(draft: dict) -> bool:
    ready_at = draft.get("ready_at")
    return ready_at is not None and (time.monotonic() - ready_at) > DRAFT_TTL_SECONDS


def _draft_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ GO LIVE", callback_data=f"go_live:{session_id}")],
            [InlineKeyboardButton("🔁 Regenerate description", callback_data=f"regenerate_desc:{session_id}")],
            [InlineKeyboardButton("❌ ABORT", callback_data=f"abort:{session_id}")],
        ]
    )


async def _reply_dead_session(query) -> None:
    await query.answer()
    await query.message.reply_text("This draft was cancelled. Please start again.")


async def _reply_expired(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    await query.answer("⏰ This draft has expired — send new photos to start again.", show_alert=True)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        "This draft expired (ready for more than 10 minutes) — send new photos to start again."
    )
    context.chat_data.clear()


def _live_draft(context: ContextTypes.DEFAULT_TYPE, session_id: str) -> dict | None:
    """Fetch the chat's draft only if it's still the SAME session that
    produced the button being tapped. Returns None for a dead/cancelled/
    already-finished session or a stale button from an earlier one — the
    caller is expected to reply accordingly rather than crash or no-op."""
    draft = context.chat_data.get("draft")
    if draft is None or draft.get("session_id") != session_id:
        return None
    return draft


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.chat_data.setdefault("draft", {})
    draft.setdefault("session_id", _new_session_id())
    draft["active_task"] = asyncio.current_task()
    raw_photos = draft.setdefault("raw_photos", [])

    largest = update.message.photo[-1]
    file = await context.bot.get_file(largest.file_id)
    raw_photos.append(bytes(await file.download_as_bytearray()))
    count_after_this_photo = len(raw_photos)

    if count_after_this_photo >= MAX_RAW_PHOTOS:
        return await _finish_photos(update, context)

    # Debounce: wait briefly, then only the invocation that still sees the
    # same photo count (i.e. no further photo arrived while it waited) is
    # the last one in the batch, and finalizes for everyone.
    await asyncio.sleep(_PHOTO_BATCH_DEBOUNCE_SECONDS)
    if context.chat_data.get("draft") is not draft:
        # Session was cancelled (or replaced) while this was asleep.
        return ConversationHandler.END
    if len(raw_photos) != count_after_this_photo:
        return WAITING_PHOTOS

    return await _finish_photos(update, context)


async def _finish_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.chat_data.get("draft")
    if draft is None:
        return ConversationHandler.END
    message = update.effective_message
    count = len(draft["raw_photos"])

    await message.reply_text(
        f"Received {count} photo(s) — that's all the photos. Looking at them now..."
    )
    draft["color"] = await ai.detect_color(draft["raw_photos"])
    draft.pop("active_task", None)

    await message.reply_text(_QUESTIONS_MESSAGE)
    return WAITING_ANSWERS


async def photo_during_answers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Already have the photo(s) for this product — please answer the 9 questions above."
    )
    return WAITING_ANSWERS


async def receive_answers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.chat_data.get("draft")
    if draft is None:
        return ConversationHandler.END
    text = update.message.text.strip()

    try:
        parsed = await ai.parse_new_product_answers(text)
    except Exception:
        logger.exception("Failed to parse new-product answers")
        await update.message.reply_text(
            "Couldn't read that — please reply with all 9 answers in one message."
        )
        return WAITING_ANSWERS

    # Deterministic shortcut — don't rely solely on the LLM catching this.
    if _ALL_POSES_RE.search(text):
        parsed["pose_request"] = {
            "mode": "specific",
            "pose_numbers": list(prompts.POSES),
            "count": 0,
        }

    invalid_sizes = [s["size"] for s in parsed["sizes"] if s["size"] not in VALID_SIZES]
    if invalid_sizes or not parsed["sizes"]:
        await update.message.reply_text(
            f"Not valid sizes: {', '.join(invalid_sizes) or '(none given)'}.\n"
            f"Rama Chikan only sells: {', '.join(VALID_SIZES)}. Please resend all 9 answers."
        )
        return WAITING_ANSWERS

    if parsed["price"] <= 0:
        await update.message.reply_text("Price must be a positive number — please resend all 9 answers.")
        return WAITING_ANSWERS

    if not (0 <= parsed["discount_pct"] < 100):
        await update.message.reply_text("Discount % must be between 0 and 100 — please resend all 9 answers.")
        return WAITING_ANSWERS

    if not parsed["categories"]:
        await update.message.reply_text(
            "Didn't catch a category — reply with For Nani, For Mom, and/or For Me "
            "(please resend all 9 answers)."
        )
        return WAITING_ANSWERS

    draft["material"] = parsed["material"]
    draft["size_quantities"] = {s["size"]: s["quantity"] for s in parsed["sizes"]}
    draft["price"] = parsed["price"]
    draft["discount_pct"] = parsed["discount_pct"]
    draft["categories"] = parsed["categories"]
    draft["is_bestseller"] = parsed["is_bestseller"]
    draft["kurti_length"] = parsed["kurti_length"]
    draft["listing_type"] = parsed["listing_type"]
    draft["compare_at_price"] = shopify_client.compute_compare_at_price(
        parsed["price"], parsed["discount_pct"]
    )

    selected, blocked = prompts.resolve_pose_selection(
        parsed["pose_request"], draft["listing_type"], _HAS_BACK_REFERENCE
    )
    if not selected and not blocked:
        await update.message.reply_text(
            "Couldn't resolve any poses from that — reply with pose numbers "
            "like \"1, 5, 3\", \"all poses\", or a count like \"4\" for "
            "question 9 (please resend all 9 answers)."
        )
        return WAITING_ANSWERS

    if blocked:
        draft["pending_selected_poses"] = selected
        block_lines = "\n".join(f"- Pose {p}: {reason}" for p, reason in blocked)
        buttons = []
        if selected:
            buttons.append(
                [InlineKeyboardButton(
                    "▶️ Proceed without these",
                    callback_data=f"proceed_blocked:{draft['session_id']}",
                )]
            )
        buttons.append(
            [InlineKeyboardButton(
                "📷 Resend answers / new photo",
                callback_data=f"cancel_blocked:{draft['session_id']}",
            )]
        )
        msg = "Can't generate some of the poses you asked for:\n" + block_lines
        if selected:
            msg += f"\n\nThe rest ({', '.join(str(p) for p in selected)}) can still be generated."
        else:
            msg += "\n\nNone of the poses you asked for can be generated as-is."
        msg += "\n\nProceed without the blocked ones, or resend?"
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_ANSWERS

    return await _generate_and_send_draft(update.message, context, draft, selected)


async def proceed_blocked_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    session_id = query.data.split(":", 1)[1]
    draft = _live_draft(context, session_id)
    if draft is None:
        await _reply_dead_session(query)
        return ConversationHandler.END

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    selected = draft.pop("pending_selected_poses", [])
    if not selected:
        await query.message.reply_text("Nothing to generate — resend the 9 answers with different poses.")
        return WAITING_ANSWERS
    return await _generate_and_send_draft(query.message, context, draft, selected)


async def cancel_blocked_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    session_id = query.data.split(":", 1)[1]
    draft = _live_draft(context, session_id)
    if draft is None:
        await _reply_dead_session(query)
        return ConversationHandler.END

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    context.chat_data.clear()
    await query.message.reply_text("Cleared — send new photos whenever you're ready.")
    return ConversationHandler.END


async def _generate_and_send_draft(message, context: ContextTypes.DEFAULT_TYPE, draft: dict, resolved_poses: list[int]) -> int:
    photo_word = "photo" if len(resolved_poses) == 1 else "photos"
    await message.reply_text(
        f"Generating {len(resolved_poses)} model {photo_word} "
        f"(poses {', '.join(str(p) for p in resolved_poses)}) — this takes "
        "a bit, I'll send them as soon as they're ready."
    )

    draft["active_task"] = asyncio.current_task()
    try:
        images, generated_poses, queued_poses = await image_gen.generate_model_images(
            draft["raw_photos"], draft["color"], draft["material"],
            draft["kurti_length"], draft["listing_type"], resolved_poses,
            draft["categories"],
        )
        draft["generated_images"] = images
        draft["generated_poses"] = generated_poses
        draft["queued_poses"] = queued_poses

        if queued_poses:
            await message.reply_text(
                f"(Poses {', '.join(str(p) for p in queued_poses)} are queued for "
                "when more image generation is enabled — only "
                f"{len(generated_poses)} generated right now to save API credits.)"
            )

        copy = await ai.generate_description(draft["color"], draft["material"], draft["listing_type"])
        draft["title"] = copy["title"]
        draft["description_html"] = copy["description_html"]
        # Starts the 10-minute clock. setdefault, not assignment: this
        # function only ever runs once per draft (the initial generation),
        # but stays defensive against being called twice for the same draft.
        draft.setdefault("ready_at", time.monotonic())
    except Exception:
        logger.exception("Image/description generation failed")
        await message.reply_text(
            "Image generation failed — nothing was published. Send the 9 answers again to retry."
        )
        return WAITING_ANSWERS
    finally:
        draft.pop("active_task", None)

    await _send_draft_preview(message, draft)
    return CONFIRMING


_TAG_RE = re.compile(r"<[^>]+>")


def _description_html_to_telegram_text(description_html: str) -> str:
    """The Shopify write always uses the real HTML (headline + paragraph) —
    this is only for showing it readably in a Telegram draft message, which
    renders Markdown, not HTML."""
    text = description_html
    text = re.sub(r"<h[1-6]>(.*?)</h[1-6]>", r"*\1*\n\n", text, flags=re.S | re.I)
    text = re.sub(r"<p>(.*?)</p>", r"\1", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    return html_lib.unescape(text).strip()


def _draft_caption(draft: dict) -> str:
    sizes_summary = ", ".join(
        f"{size}: {qty}" for size, qty in draft["size_quantities"].items()
    )
    unlisted = [s for s in VALID_SIZES if s not in draft["size_quantities"]]
    price_line = f"₹{draft['price']:.0f}"
    if draft.get("compare_at_price"):
        price_line = f"~₹{draft['compare_at_price']:.0f}~ ₹{draft['price']:.0f}"

    lines = [
        f"*{draft['title']}*",
        "",
        _description_html_to_telegram_text(draft["description_html"]),
        "",
        f"Material: {draft['material']}",
        f"Category: {', '.join(draft['categories'])}",
        f"Length: {draft['kurti_length'].capitalize()}",
        "Listing: " + (
            "Kurti + Pyjama Set"
            if draft["listing_type"] == "kurti_pyjama_set"
            else "Kurti Only (bottom shown is styling reference, not included)"
        ),
        "Poses used: " + ", ".join(str(p) for p in draft["generated_poses"]),
        f"Sizes in stock: {sizes_summary}",
    ]
    if draft.get("queued_poses"):
        lines.append(
            "Poses queued (not generated yet — API credit cap): "
            + ", ".join(str(p) for p in draft["queued_poses"])
        )
    if unlisted:
        lines.append(f"Out of stock (not purchasable): {', '.join(unlisted)}")
    lines.append(f"Price: {price_line}")
    if draft.get("is_bestseller"):
        lines.append("⭐ Marked as bestseller")
    lines.append("")
    lines.append("Nothing goes live until you tap GO LIVE.")
    return "\n".join(lines)


async def _send_draft_preview(message, draft: dict) -> None:
    media = [
        InputMediaPhoto(io.BytesIO(img), filename=f"angle-{i}.png")
        for i, img in enumerate(draft["generated_images"])
    ]
    await message.reply_media_group(media=media)
    await message.reply_text(
        _draft_caption(draft), reply_markup=_draft_keyboard(draft["session_id"]), parse_mode="Markdown"
    )


async def regenerate_description_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    session_id = query.data.split(":", 1)[1]
    draft = _live_draft(context, session_id)
    if draft is None:
        await _reply_dead_session(query)
        return ConversationHandler.END
    if _draft_expired(draft):
        await _reply_expired(query, context)
        return ConversationHandler.END

    await query.answer("Rewriting description...")
    copy = await ai.generate_description(
        draft["color"], draft["material"], draft["listing_type"], regenerate=True
    )
    draft["title"] = copy["title"]
    draft["description_html"] = copy["description_html"]

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        _draft_caption(draft), reply_markup=_draft_keyboard(session_id), parse_mode="Markdown"
    )
    return CONFIRMING


async def go_live_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    session_id = query.data.split(":", 1)[1]
    draft = _live_draft(context, session_id)
    if draft is None:
        await _reply_dead_session(query)
        return ConversationHandler.END
    if _draft_expired(draft):
        await _reply_expired(query, context)
        return ConversationHandler.END

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Publishing to Shopify...")

    try:
        image_urls = []
        for i, img_bytes in enumerate(draft["generated_images"]):
            url = await shopify_client.upload_image(img_bytes, f"{draft['title']}-{i}.png")
            image_urls.append(url)

        product = await shopify_client.create_live_product(
            title=draft["title"],
            description_html=draft["description_html"],
            tags=[draft["material"], draft["color"], *draft["categories"]],
            price=draft["price"],
            compare_at_price=draft["compare_at_price"],
            size_quantities=draft["size_quantities"],
            image_resource_urls=image_urls,
            material=draft["material"],
            categories=draft["categories"],
            is_bestseller=draft["is_bestseller"],
        )
    except Exception:
        logger.exception("Failed to publish product to Shopify")
        await query.message.reply_text(
            "Something went wrong publishing this to Shopify — nothing went live. "
            "Tap GO LIVE again to retry, or ABORT to discard this draft."
        )
        return CONFIRMING

    await query.message.reply_text(
        f"🟢 Live: *{product['title']}*\n"
        f"https://ramachikan.com/products/{product['handle']}",
        parse_mode="Markdown",
    )
    context.chat_data.clear()
    return ConversationHandler.END


async def abort_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    session_id = query.data.split(":", 1)[1]
    draft = _live_draft(context, session_id)
    if draft is None:
        await _reply_dead_session(query)
        return ConversationHandler.END

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    who = update.effective_user.full_name if update.effective_user else "someone"
    await query.message.reply_text(f"Aborted by {who} — nothing was published.")
    context.chat_data.clear()
    return ConversationHandler.END


def build_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, receive_photo)],
        states={
            WAITING_PHOTOS: [MessageHandler(filters.PHOTO, receive_photo)],
            WAITING_ANSWERS: [
                MessageHandler(filters.PHOTO, photo_during_answers),
                CallbackQueryHandler(proceed_blocked_tap, pattern="^proceed_blocked:"),
                CallbackQueryHandler(cancel_blocked_tap, pattern="^cancel_blocked:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_answers),
            ],
            CONFIRMING: [
                CallbackQueryHandler(go_live_tap, pattern="^go_live:"),
                CallbackQueryHandler(regenerate_description_tap, pattern="^regenerate_desc:"),
                CallbackQueryHandler(abort_tap, pattern="^abort:"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=1800,
        # per_user=False: ONE shared session per chat, not one per Telegram
        # user. This bot runs in a group with several members — any of them
        # must be able to see/continue/cancel the same in-progress session,
        # and Telegram's "send anonymously" group option makes each message's
        # effective_user a different pseudo-identity anyway, which silently
        # breaks per-user keying even for the original sender's own /cancel.
        # All session state lives in context.chat_data, never user_data.
        per_user=False,
    )
