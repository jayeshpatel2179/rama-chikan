import asyncio
import html as html_lib
import io
import logging
import re
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import ai, image_gen, instagram, prompts, shopify_client
from bot.config import VALID_SIZES
from bot.handlers.cancel import cancel

logger = logging.getLogger(__name__)

(
    WAITING_FRONT_PHOTO,
    WAITING_BACK_PHOTO,
    WAITING_PYJAMA_CHOICE,
    WAITING_PYJAMA_PHOTO,
    WAITING_ANSWERS,
    CONFIRMING,
) = range(6)

# Photo intake is sequential, one at a time — the bot explicitly asks for
# the FRONT photo first, waits for it, then explicitly asks for the BACK
# photo and waits for that before moving on to the 10 questions. Replaces an
# earlier batch/debounce design (send several photos at once, guess which is
# front/back by position) that turned out to hurt generation accuracy —
# always sending both photos as explicit, individually-confirmed
# draft["front_photo"] / draft["back_photo"] removes that guesswork
# entirely, and there's no ambiguity left to need a swap button for. Poses
# 10/11 (back view) use ONLY the back photo as their garment reference
# (bot/image_gen.py), never blended with the front.

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
    12: "Knee-length front (needs pose 1 also selected)",
    13: "Knee-length back (needs pose 10 also selected)",
}
assert set(_POSE_MENU_LABELS) == set(prompts.POSES), "pose menu is out of sync with bot.prompts.POSES"
_POSE_MENU = "\n".join(f"{n} = {label}" for n, label in _POSE_MENU_LABELS.items())

# Short menu labels for Question 10 (premium editorial poses) — same
# convention as _POSE_MENU_LABELS above, kept in sync with prompts.PREMIUM_POSES.
_PREMIUM_POSE_MENU_LABELS = {
    "P1": "Full-length standing, styled set",
    "P2": "Waist-up, looking to the side",
    "P3": "Leaning on wall, hand in hair",
    "P4": "Full-length in arched doorway",
    "P5": "Leaning on pillar, hands clasped",
    "P6": "Reclining on window seat",
    "P7": "Seated close-up with bolster cushion",
    "P8": "Seated on floor, hand on cheek",
    "P9": "Back full-length",
    "P10": "Back over-the-shoulder",
    "P11": "Embroidery close-up",
    "P12": "Knee-length front (needs P1 also selected)",
    "P13": "Knee-length back (needs P9 also selected)",
}
assert set(_PREMIUM_POSE_MENU_LABELS) == set(prompts.PREMIUM_POSES), (
    "premium pose menu is out of sync with bot.prompts.PREMIUM_POSES"
)
_PREMIUM_POSE_MENU = "\n".join(f"{n} = {label}" for n, label in _PREMIUM_POSE_MENU_LABELS.items())

_QUESTIONS_MESSAGE = (
    "A few quick questions — reply to all of them in ONE message:\n\n"
    "1. Material type (e.g. rayon, georgette, chikankari work)\n"
    "2. Sizes with quantity — e.g. \"3 of XS / 1 of S\". Sizes you don't mention "
    "will show on the site as out of stock.\n"
    "3. Price (the real selling price, e.g. 1500)\n"
    "4. Discount % to display (e.g. 20%, or say \"none\")\n"
    "5. Category — which collections should this go into? (name one or more)\n"
    "   1 = Premium\n"
    "   2 = Kurtis\n"
    "   3 = Kurti Sets\n"
    "   4 = For Nani/Dadi\n"
    "   5 = For Mom\n"
    "   6 = For Me\n"
    "   7 = On Sale\n"
    "   (On Sale is added automatically whenever question 4's discount is "
    "above 0% — you don't need to pick it yourself, and it won't be added "
    "if there's no discount.)\n"
    "6. Is this a best-selling kurti? (yes/no)\n"
    "7. Kurti length — short or long?\n"
    "8. What's in this listing — kurti + pyjama set, or kurti only?\n"
    "9. How many images, and which poses? Reply with pose numbers, e.g. "
    "\"1, 5, 3\" — or type \"all poses\" for all 13 — or just give a number "
    "like \"4\" and I'll pick the best combination. (Poses 12/13 are "
    "knee-length crops of poses 1/10 — include 1 or 10 too if you want "
    "them.)\n" + _POSE_MENU + "\n\n"
    "10. Want premium editorial shots instead? Reply with premium pose "
    "numbers (e.g. \"P1, P6, P9\"), or skip to use the standard poses from "
    "Q9. (P12/P13 are knee-length crops of P1/P9 — include P1 or P9 too if "
    "you want them.)\n" + _PREMIUM_POSE_MENU + "\n\nAlso accept \"all "
    "premium\" to generate all 13."
)

_ALL_POSES_RE = re.compile(r"\ball\s+poses\b", re.I)
_ALL_PREMIUM_RE = re.compile(r"\ball\s+premium\b", re.I)

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


def _draft_keyboard(session_id: str, draft: dict) -> InlineKeyboardMarkup:
    """Four independent buttons (Part 6, 2026-09-16). GO LIVE — SHOPIFY and
    Go IG + FB are independent: pressing one never disables or
    consumes the other. Once a GO LIVE button succeeds it's relabeled with
    a checkmark and routed to a no-op callback so it can't be double-fired
    — the row itself is never removed, so the other platform stays
    pressable. REGENERATE CAP & # and ABORT are always available."""
    if draft.get("shopify_published"):
        shopify_button = InlineKeyboardButton("✅ Live on Shopify", callback_data=f"noop:{session_id}")
    else:
        shopify_button = InlineKeyboardButton(
            "🟢 GO LIVE — SHOPIFY", callback_data=f"go_live_shopify:{session_id}"
        )

    if draft.get("instagram_published"):
        instagram_button = InlineKeyboardButton("✅ Posted IG + FB", callback_data=f"noop:{session_id}")
    else:
        instagram_button = InlineKeyboardButton(
            "📸 Go IG + FB", callback_data=f"go_live_instagram:{session_id}"
        )

    return InlineKeyboardMarkup(
        [
            [shopify_button, instagram_button],
            [InlineKeyboardButton("🔁 REGENERATE CAP & #", callback_data=f"regen_caption:{session_id}")],
            [InlineKeyboardButton("❌ ABORT", callback_data=f"abort:{session_id}")],
        ]
    )


async def noop_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback target for an already-done GO LIVE button — acknowledges
    the tap without re-firing the action, per Part 6's "mark it as done...
    so it cannot be double-fired." Stays in CONFIRMING; doesn't touch the draft."""
    await update.callback_query.answer("Already done ✅")
    return CONFIRMING


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


async def receive_front_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.chat_data.setdefault("draft", {})
    draft.setdefault("session_id", _new_session_id())

    largest = update.message.photo[-1]
    file = await context.bot.get_file(largest.file_id)
    draft["front_photo"] = bytes(await file.download_as_bytearray())

    await update.message.reply_text("Got the front photo. Now send the back photo.")
    return WAITING_BACK_PHOTO


async def prompt_for_front_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please send the front photo of the garment first.")
    return WAITING_FRONT_PHOTO


def _pyjama_choice_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Yes — I'll send the pyjama photo", callback_data=f"pyjama_yes:{session_id}")],
            [InlineKeyboardButton("No — kurti only", callback_data=f"pyjama_no:{session_id}")],
        ]
    )


async def receive_back_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.chat_data.get("draft")
    if draft is None:
        return ConversationHandler.END

    draft["active_task"] = asyncio.current_task()
    largest = update.message.photo[-1]
    file = await context.bot.get_file(largest.file_id)
    draft["back_photo"] = bytes(await file.download_as_bytearray())
    draft["raw_photos"] = [draft["front_photo"], draft["back_photo"]]

    await update.message.reply_text("Got the back photo. Looking at them now...")
    draft["color"] = await ai.detect_color(draft["raw_photos"])
    draft.pop("active_task", None)

    await update.message.reply_text(
        "Does this listing include a pyjama?",
        reply_markup=_pyjama_choice_keyboard(draft["session_id"]),
    )
    return WAITING_PYJAMA_CHOICE


async def prompt_for_back_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Got the front photo already — now send the back photo.")
    return WAITING_BACK_PHOTO


async def pyjama_yes_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    session_id = query.data.split(":", 1)[1]
    draft = _live_draft(context, session_id)
    if draft is None:
        await _reply_dead_session(query)
        return ConversationHandler.END

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Send the pyjama photo.")
    return WAITING_PYJAMA_PHOTO


async def pyjama_no_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    session_id = query.data.split(":", 1)[1]
    draft = _live_draft(context, session_id)
    if draft is None:
        await _reply_dead_session(query)
        return ConversationHandler.END

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(_QUESTIONS_MESSAGE)
    return WAITING_ANSWERS


async def receive_pyjama_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # PYJAMA_REFERENCE — the real pyjama photo, kept separate from
    # front_photo/back_photo (FRONT_REFERENCE/BACK_REFERENCE) so the
    # generation pipeline (bot/image_gen.py) can attach it as its own
    # reference image without confusing it for the kurti garment itself.
    draft = context.chat_data.get("draft")
    if draft is None:
        return ConversationHandler.END

    largest = update.message.photo[-1]
    file = await context.bot.get_file(largest.file_id)
    draft["pyjama_photo"] = bytes(await file.download_as_bytearray())

    await update.message.reply_text("Got the pyjama photo.")
    await update.message.reply_text(_QUESTIONS_MESSAGE)
    return WAITING_ANSWERS


async def prompt_for_pyjama_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please send the pyjama photo to continue.")
    return WAITING_PYJAMA_PHOTO


async def photo_during_answers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Already have the photo(s) for this product — please answer the 10 questions above."
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
            "Couldn't read that — please reply with all 10 answers in one message."
        )
        return WAITING_ANSWERS

    # Deterministic shortcuts — don't rely solely on the LLM catching these.
    if _ALL_POSES_RE.search(text):
        parsed["pose_request"] = {
            "mode": "specific",
            "pose_numbers": list(prompts.POSES),
            "count": 0,
        }
    if _ALL_PREMIUM_RE.search(text):
        parsed["premium_pose_numbers"] = list(prompts.PREMIUM_POSES)

    invalid_sizes = [s["size"] for s in parsed["sizes"] if s["size"] not in VALID_SIZES]
    if invalid_sizes or not parsed["sizes"]:
        await update.message.reply_text(
            f"Not valid sizes: {', '.join(invalid_sizes) or '(none given)'}.\n"
            f"Rama Chikan only sells: {', '.join(VALID_SIZES)}. Please resend all 10 answers."
        )
        return WAITING_ANSWERS

    if parsed["price"] <= 0:
        await update.message.reply_text("Price must be a positive number — please resend all 10 answers.")
        return WAITING_ANSWERS

    if not (0 <= parsed["discount_pct"] < 100):
        await update.message.reply_text("Discount % must be between 0 and 100 — please resend all 10 answers.")
        return WAITING_ANSWERS

    if not parsed["categories"]:
        await update.message.reply_text(
            "Didn't catch a category — reply with one or more of Premium, Kurtis, "
            "Kurti Sets, For Nani/Dadi, For Mom, For Me, On Sale (please resend "
            "all 10 answers)."
        )
        return WAITING_ANSWERS

    # Normalize old/renamed wording the LLM might still pass through, then
    # derive On Sale deterministically from the discount answer rather than
    # trusting whatever the owner did or didn't type for question 5 — per
    # spec, On Sale must never depend on the owner remembering to pick it,
    # and must never appear when there's no discount, no exceptions.
    _CATEGORY_RENAMES = {"for nani": "For Nani/Dadi", "kurtas": "Kurtis"}
    categories = [
        _CATEGORY_RENAMES.get(c.strip().lower(), c.strip()) for c in parsed["categories"]
    ]
    categories = [c for c in categories if c.lower() != "on sale"]
    if parsed["discount_pct"] > 0:
        categories.append("On Sale")

    draft["material"] = parsed["material"]
    draft["size_quantities"] = {s["size"]: s["quantity"] for s in parsed["sizes"]}
    draft["price"] = parsed["price"]
    draft["discount_pct"] = parsed["discount_pct"]
    draft["categories"] = categories
    draft["is_bestseller"] = parsed["is_bestseller"]
    draft["kurti_length"] = parsed["kurti_length"]
    draft["listing_type"] = parsed["listing_type"]
    draft["compare_at_price"] = shopify_client.compute_compare_at_price(
        parsed["price"], parsed["discount_pct"]
    )

    selected, blocked = prompts.resolve_pose_selection(
        parsed["pose_request"], draft["listing_type"], bool(draft.get("back_photo"))
    )
    premium_selected, premium_blocked = prompts.resolve_premium_pose_selection(
        parsed["premium_pose_numbers"], bool(draft.get("back_photo"))
    )
    blocked = blocked + premium_blocked

    if not selected and not premium_selected and not blocked:
        await update.message.reply_text(
            "Couldn't resolve any poses from that — reply with pose numbers "
            "like \"1, 5, 3\", \"all poses\", or a count like \"4\" for "
            "question 9, and/or premium pose numbers like \"P1, P6\" for "
            "question 10 (please resend all 10 answers)."
        )
        return WAITING_ANSWERS

    if blocked:
        # premium_selected only ever excludes poses resolve_premium_pose_selection
        # itself blocked (missing back reference, or — since 2026-09-26 — a
        # missing P1/P9 crop dependency for P12/P13) — folded in here
        # unconditionally so the rest still generate once the owner resolves
        # whatever's blocked below.
        draft["pending_selected_poses"] = selected + premium_selected
        block_lines = "\n".join(f"- Pose {p}: {reason}" for p, reason in blocked)
        buttons = []
        if selected or premium_selected:
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
        remaining = selected + premium_selected
        if remaining:
            msg += f"\n\nThe rest ({', '.join(str(p) for p in remaining)}) can still be generated."
        else:
            msg += "\n\nNone of the poses you asked for can be generated as-is."
        msg += "\n\nProceed without the blocked ones, or resend?"
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_ANSWERS

    return await _generate_and_send_draft(update.message, context, draft, selected + premium_selected)


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
        await query.message.reply_text("Nothing to generate — resend the 10 answers with different poses.")
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


async def _generate_and_send_draft(message, context: ContextTypes.DEFAULT_TYPE, draft: dict, resolved_poses: list) -> int:
    photo_word = "photo" if len(resolved_poses) == 1 else "photos"
    await message.reply_text(
        f"Generating {len(resolved_poses)} model {photo_word} "
        f"(poses {', '.join(str(p) for p in resolved_poses)}) — this takes "
        "a bit, I'll send them as soon as they're ready."
    )

    draft["active_task"] = asyncio.current_task()
    try:
        images, generated_poses, queued_poses = await image_gen.generate_model_images(
            draft["raw_photos"], draft.get("front_photo"), draft.get("back_photo"),
            draft.get("pyjama_photo"),
            draft["color"], draft["material"],
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
        draft["instagram_caption"] = await ai.generate_instagram_caption(
            images[0], draft["color"], draft["material"], draft["listing_type"]
        )
        # Starts the 10-minute clock. setdefault, not assignment: this
        # function only ever runs once per draft (the initial generation),
        # but stays defensive against being called twice for the same draft.
        draft.setdefault("ready_at", time.monotonic())
    except image_gen.MissingReferenceError as exc:
        logger.warning("Missing reference photo for pose %s: %s", exc.pose_id, exc)
        await message.reply_text(
            f"Can't generate pose {exc.pose_id} — the {exc.reference_kind.upper()} "
            "reference photo is missing. Send that photo and resend the 10 answers "
            "to retry (nothing else was affected)."
        )
        return WAITING_ANSWERS
    except Exception:
        logger.exception("Image/description generation failed")
        await message.reply_text(
            "Image generation failed — nothing was published. Send the 10 answers again to retry."
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
            + (" (real pyjama reference photo used)" if draft.get("pyjama_photo") else "")
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
    lines.append(
        "— IG/FB caption (posted with all photos as a carousel via Go IG + FB) —"
        if len(draft.get("generated_images", [])) > 1
        else "— IG/FB caption (posted with Go IG + FB) —"
    )
    lines.append(draft.get("instagram_caption", "(caption not generated)"))
    lines.append("")
    status_bits = []
    if draft.get("shopify_published"):
        status_bits.append(f"✅ Live on Shopify: {draft.get('shopify_url', '')}")
    if draft.get("instagram_published"):
        status_bits.append(
            f"✅ Posted on Instagram: {draft.get('instagram_url') or '(link pending)'}"
        )
    if draft.get("facebook_published"):
        status_bits.append(
            f"✅ Posted on Facebook: {draft.get('facebook_url') or '(link pending)'}"
        )
    if status_bits:
        lines.extend(status_bits)
        lines.append("")
    lines.append("Nothing goes live until you tap a GO LIVE button.")
    return "\n".join(lines)


# Telegram's sendMediaGroup rejects more than 10 items in one call ("Too
# many messages to send as an album") — this is what broke once a product
# generated more than 10 images (e.g. "all poses" alone is already 11, or
# any standard+premium combination past 10). It also requires at least 2
# items per call, so a single generated image can't go through
# sendMediaGroup at all and is sent as a plain photo instead.
_MAX_MEDIA_GROUP_SIZE = 10


def _chunk_media(media: list) -> list[list]:
    """Split into chunks of at most _MAX_MEDIA_GROUP_SIZE. Never leaves a
    trailing chunk of exactly 1 item — borrows one back from the previous
    chunk instead, since sendMediaGroup requires at least 2 per call."""
    chunks: list[list] = []
    remaining = list(media)
    while len(remaining) > _MAX_MEDIA_GROUP_SIZE:
        chunks.append(remaining[:_MAX_MEDIA_GROUP_SIZE])
        remaining = remaining[_MAX_MEDIA_GROUP_SIZE:]
    chunks.append(remaining)
    if len(chunks) > 1 and len(chunks[-1]) == 1:
        chunks[-1].insert(0, chunks[-2].pop())
    return chunks


async def _send_draft_preview(message, draft: dict) -> None:
    images = draft["generated_images"]
    if len(images) == 1:
        await message.reply_photo(photo=io.BytesIO(images[0]))
    else:
        media = [
            InputMediaPhoto(io.BytesIO(img), filename=f"angle-{i}.png")
            for i, img in enumerate(images)
        ]
        for chunk in _chunk_media(media):
            await message.reply_media_group(media=chunk)
    await message.reply_text(
        _draft_caption(draft), reply_markup=_draft_keyboard(draft["session_id"], draft), parse_mode="Markdown"
    )


async def regen_caption_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """REGENERATE CAP & # (Part 6) — regenerates ONLY the Instagram caption
    and hashtags. Images, product description, price, sizes and categories
    all stay untouched. Re-sends the draft with the new caption, per spec."""
    query = update.callback_query
    session_id = query.data.split(":", 1)[1]
    draft = _live_draft(context, session_id)
    if draft is None:
        await _reply_dead_session(query)
        return ConversationHandler.END
    if _draft_expired(draft):
        await _reply_expired(query, context)
        return ConversationHandler.END

    await query.answer("Rewriting caption...")
    draft["instagram_caption"] = await ai.generate_instagram_caption(
        draft["generated_images"][0], draft["color"], draft["material"], draft["listing_type"]
    )

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        _draft_caption(draft), reply_markup=_draft_keyboard(session_id, draft), parse_mode="Markdown"
    )
    return CONFIRMING


async def go_live_shopify_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """GO LIVE — SHOPIFY (Part 6). Publishes to Shopify exactly as before —
    unchanged logic. Independent of Instagram: does not touch it, and
    doesn't clear the session, so GO LIVE — INSTAGRAM stays pressable
    afterward (in either order)."""
    query = update.callback_query
    session_id = query.data.split(":", 1)[1]
    draft = _live_draft(context, session_id)
    if draft is None:
        await _reply_dead_session(query)
        return ConversationHandler.END
    if _draft_expired(draft):
        await _reply_expired(query, context)
        return ConversationHandler.END
    if draft.get("shopify_published"):
        await query.answer("Already live on Shopify ✅")
        return CONFIRMING

    await query.answer()
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
            "Tap GO LIVE — SHOPIFY again to retry, or ABORT to discard this draft."
        )
        return CONFIRMING

    draft["shopify_published"] = True
    draft["shopify_url"] = f"https://ramachikan.com/products/{product['handle']}"
    await query.edit_message_reply_markup(reply_markup=_draft_keyboard(session_id, draft))
    await query.message.reply_text(
        f"🟢 Live on Shopify: *{product['title']}*\n{draft['shopify_url']}",
        parse_mode="Markdown",
    )
    return CONFIRMING


async def go_live_instagram_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """"Go IG + FB" (Part 6; Facebook added 2026-09-23). Posts ALL generated
    images (as one carousel per platform when there are 2+, max 10 each)
    plus caption/hashtags via upload-post, to Instagram AND Facebook in one
    call. Independent of Shopify: does not touch it, and doesn't clear the
    session either way. The two platforms are reported separately — one can
    succeed while the other fails (e.g. the Facebook Page isn't connected in
    Upload-Post yet) — and the button is only marked done (draft["instagram_published"])
    if at least one platform actually posted, so a fully-failed attempt is
    always retryable without risking a duplicate post on the platform that
    already worked."""
    query = update.callback_query
    session_id = query.data.split(":", 1)[1]
    draft = _live_draft(context, session_id)
    if draft is None:
        await _reply_dead_session(query)
        return ConversationHandler.END
    if _draft_expired(draft):
        await _reply_expired(query, context)
        return ConversationHandler.END
    if draft.get("instagram_published"):
        await query.answer("Already posted ✅")
        return CONFIRMING

    await query.answer()
    images = draft["generated_images"]
    count = min(len(images), instagram.MAX_CAROUSEL_ITEMS)
    await query.message.reply_text(
        "Posting to Instagram + Facebook..." if count == 1
        else f"Posting {count} photos to Instagram + Facebook as a carousel..."
    )

    draft["active_task"] = asyncio.current_task()
    try:
        results = await instagram.post_images(images, draft["instagram_caption"])
    except instagram.UploadPostError as exc:
        logger.exception("Failed to publish product to Instagram/Facebook")
        await query.message.reply_text(
            f"Something went wrong posting — nothing was posted.\n{exc}\n"
            "Tap Go IG + FB again to retry, or ABORT to discard this draft."
        )
        return CONFIRMING
    except asyncio.CancelledError:
        raise
    finally:
        draft.pop("active_task", None)

    if len(images) > instagram.MAX_CAROUSEL_ITEMS:
        await query.message.reply_text(
            f"Note: each platform's carousel holds at most {instagram.MAX_CAROUSEL_ITEMS} photos, "
            f"so only the first {instagram.MAX_CAROUSEL_ITEMS} of your {len(images)} were posted."
        )

    def _report(label: str, emoji: str, r) -> str:
        if r.success:
            return f"{emoji} Posted to {label}:\n{r.url}" if r.url else (
                f"{emoji} Posted to {label} — Upload-Post confirmed it, "
                "but hasn't handed back the direct link yet."
            )
        if r.pending:
            return (
                f"⏳ {label} is still processing at Upload-Post — not confirmed posted yet. "
                "Check the Upload-Post dashboard in a bit, or tap Go IG + FB again shortly."
            )
        text = f"❌ {label} post failed: {r.error}"
        if label == "Facebook":
            text += (
                "\nIf this is the first attempt, check that the Rama Chikan Facebook "
                "Page is connected to the Upload-Post profile."
            )
        return text

    ig, fb = results["instagram"], results["facebook"]

    await query.message.reply_text(_report("Instagram", "📸", ig))
    if ig.success:
        draft["instagram_published"] = True
        draft["instagram_url"] = ig.url

    await query.message.reply_text(_report("Facebook", "📘", fb))
    if fb.success:
        draft["facebook_published"] = True
        draft["facebook_url"] = fb.url

    if ig.success or fb.success:
        # Telegram refuses an edit whose markup is byte-identical to what's
        # already showing (BadRequest "Message is not modified") -- this is
        # cosmetic only (the checkmark relabel), the actual posting result
        # was already reported above via reply_text, so swallow it rather
        # than crash the handler and leave the conversation in a broken
        # state (see the 2026-09-23 bug report this fixed).
        try:
            await query.edit_message_reply_markup(reply_markup=_draft_keyboard(session_id, draft))
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
    else:
        await query.message.reply_text("Nothing posted. Tap Go IG + FB again to retry.")
    return CONFIRMING


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
    already_done = []
    if draft.get("shopify_published"):
        already_done.append("Shopify")
    if draft.get("instagram_published"):
        already_done.append("Instagram")
    note = f"already live on {', '.join(already_done)} before this" if already_done else "nothing was published"
    await query.message.reply_text(f"Aborted by {who} — {note}.")
    context.chat_data.clear()
    return ConversationHandler.END


def build_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, receive_front_photo)],
        states={
            WAITING_FRONT_PHOTO: [
                MessageHandler(filters.PHOTO, receive_front_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_for_front_photo),
            ],
            WAITING_BACK_PHOTO: [
                MessageHandler(filters.PHOTO, receive_back_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_for_back_photo),
            ],
            WAITING_PYJAMA_CHOICE: [
                CallbackQueryHandler(pyjama_yes_tap, pattern="^pyjama_yes:"),
                CallbackQueryHandler(pyjama_no_tap, pattern="^pyjama_no:"),
            ],
            WAITING_PYJAMA_PHOTO: [
                MessageHandler(filters.PHOTO, receive_pyjama_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_for_pyjama_photo),
            ],
            WAITING_ANSWERS: [
                MessageHandler(filters.PHOTO, photo_during_answers),
                CallbackQueryHandler(proceed_blocked_tap, pattern="^proceed_blocked:"),
                CallbackQueryHandler(cancel_blocked_tap, pattern="^cancel_blocked:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_answers),
            ],
            CONFIRMING: [
                CallbackQueryHandler(go_live_shopify_tap, pattern="^go_live_shopify:"),
                CallbackQueryHandler(go_live_instagram_tap, pattern="^go_live_instagram:"),
                CallbackQueryHandler(regen_caption_tap, pattern="^regen_caption:"),
                CallbackQueryHandler(noop_tap, pattern="^noop:"),
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
