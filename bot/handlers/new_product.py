import asyncio
import html as html_lib
import io
import logging
import re
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import ai, image_gen, instagram, prompts, shopify_client, state
from bot.config import VALID_SIZES
from bot.handlers.cancel import cancel

logger = logging.getLogger(__name__)

(
    CHOOSING_FLOW,
    WAITING_FRONT_PHOTO,
    WAITING_BACK_PHOTO,
    WAITING_PYJAMA_PHOTO,
    WAITING_DUPATTA_PHOTO,
    WAITING_BOTTOMS_FRONT_PHOTO,
    WAITING_BOTTOMS_BACK_PHOTO,
    WAITING_ANSWERS,
    CONFIRMING,
) = range(9)

# draft["listing_type"] values — set once, at the button tap that starts the
# session (2026-09-29 four-button flow restructure), instead of being asked
# as its own question partway through like the old single-flow design.
# Reused as-is by bot.prompts / bot.image_gen / bot.ai / bot.shopify_client,
# which all already key off these exact strings.
FLOW_KURTI_ONLY = "kurti_only"
FLOW_KURTI_PYJAMA_SET = "kurti_pyjama_set"
FLOW_DUPATTA = "dupatta"
FLOW_WOMEN_BOTTOMS = "women_bottoms"

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
    1: "Front full",
    2: "Front waist-up",
    3: "Embroidery close-up",
    4: "Bottom only",
    5: "Side 3/4 full",
    6: "Hem+footwear",
    7: "3/4 looking down",
    8: "Waist-up soft gaze",
    9: "Waist-up side gaze",
    10: "Back full",
    11: "Back over-shoulder",
    12: "Knee front (+1)",
    13: "Knee back (+10)",
}
assert set(_POSE_MENU_LABELS) == set(prompts.POSES), "pose menu is out of sync with bot.prompts.POSES"
# Grouped a few per line (2026-09-29) instead of one long "·"-joined line —
# a dense single line was hard for the shop owner to scan on a phone.
_POSE_MENU_LINES = [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11], [12, 13]]
assert {n for line in _POSE_MENU_LINES for n in line} == set(_POSE_MENU_LABELS), (
    "pose menu line groups are out of sync with _POSE_MENU_LABELS"
)
_POSE_MENU = "\n".join(
    " · ".join(f"*{n}* {_POSE_MENU_LABELS[n]}" for n in line) for line in _POSE_MENU_LINES
)

# Short menu labels for Question 10 (premium editorial poses) — same
# convention as _POSE_MENU_LABELS above, kept in sync with prompts.PREMIUM_POSES.
_PREMIUM_POSE_MENU_LABELS = {
    "P1": "Full standing set",
    "P2": "Waist-up side",
    "P3": "Wall lean/hair",
    "P4": "Doorway full",
    "P5": "Pillar lean",
    "P6": "Window seat",
    "P7": "Seated w/bolster",
    "P8": "Floor seated/cheek",
    "P9": "Back full",
    "P10": "Back over-shoulder",
    "P11": "Embroidery close-up",
    "P12": "Knee front (+P1)",
    "P13": "Knee back (+P9)",
}
assert set(_PREMIUM_POSE_MENU_LABELS) == set(prompts.PREMIUM_POSES), (
    "premium pose menu is out of sync with bot.prompts.PREMIUM_POSES"
)
_PREMIUM_POSE_MENU_LINES = [
    ["P1", "P2", "P3"], ["P4", "P5", "P6"], ["P7", "P8"], ["P9", "P10", "P11"], ["P12", "P13"],
]
assert {n for line in _PREMIUM_POSE_MENU_LINES for n in line} == set(_PREMIUM_POSE_MENU_LABELS), (
    "premium pose menu line groups are out of sync with _PREMIUM_POSE_MENU_LABELS"
)
_PREMIUM_POSE_MENU = "\n".join(
    " · ".join(f"*{n}* {_PREMIUM_POSE_MENU_LABELS[n]}" for n in line) for line in _PREMIUM_POSE_MENU_LINES
)

_KURTI_QUESTIONS_MESSAGE = (
    "Quick details: reply all in ONE message:\n\n"
    "🧵 *1)* Material (e.g. rayon, chikankari)\n"
    "📏 *2)* Sizes + qty (e.g. \"3 XS, 1 S\"). Unlisted sizes = out of stock\n"
    "💰 *3)* Price (₹)\n"
    "🏷️ *4)* Discount % (or \"none\")\n"
    "🗂️ *5)* Category: *1* Premium / *2* Kurtis / *3* Kurti Sets / *4* Nani-Dadi / "
    "*5* Mom / *6* Me / *7* On Sale (auto-added if discount > 0, skip it)\n"
    "⭐ *6)* Bestseller? y/n\n"
    "↕️ *7)* Length: short/long\n"
    "📸 *8)* Poses: numbers (e.g. \"1,5,3\"), \"all poses\" for all 13, or a "
    "count (e.g. \"4\"). Poses *12*/*13* need pose *1*/*10* also selected:\n"
    + _POSE_MENU + "\n\n"
    "✨ *9)* Premium instead? P-numbers (e.g. \"P1,P6,P9\"), \"all premium,\" or "
    "skip. P*12*/P*13* need P*1*/P*9* also selected:\n" + _PREMIUM_POSE_MENU
)

# --- Dupatta question set (Button 3, 2026-09-29) ----------------------------
_DUPATTA_POSE_MENU_LABELS = {
    1: "Full-length drape",
    2: "Mid-length view",
    3: "Material/embroidery close-up",
    4: "Draped over shoulder",
    5: "Held out to show flow",
}
assert set(_DUPATTA_POSE_MENU_LABELS) == set(prompts.DUPATTA_POSES), (
    "dupatta pose menu is out of sync with bot.prompts.DUPATTA_POSES"
)
_DUPATTA_POSE_MENU = " · ".join(f"*{n}* {label}" for n, label in _DUPATTA_POSE_MENU_LABELS.items())

_DUPATTA_QUESTIONS_MESSAGE = (
    "Quick details: reply all in ONE message:\n\n"
    "🧵 *1)* Material (e.g. chiffon, cotton, chanderi, chikankari)\n"
    "📏 *2)* Length: 2.25m / 2.50m / 2.75m\n"
    "💰 *3)* Price (₹)\n"
    "🏷️ *4)* Discount % (or \"none\")\n"
    "⭐ *5)* Bestseller? y/n\n"
    "📸 *6)* Poses: numbers (e.g. \"1,3\"), \"all poses\" for all 5, or a count:\n"
    + _DUPATTA_POSE_MENU
)

# --- Women Bottoms question set (Button 4, 2026-09-29) ----------------------
_BOTTOMS_POSE_MENU_LABELS = {
    1: "Front full-length",
    2: "Front hem+footwear close-up",
    3: "Side hem+footwear close-up",
    4: "Back full-length",
    5: "Waist/fit close-up",
}
assert set(_BOTTOMS_POSE_MENU_LABELS) == set(prompts.BOTTOMS_POSES), (
    "bottoms pose menu is out of sync with bot.prompts.BOTTOMS_POSES"
)
_BOTTOMS_POSE_MENU = " · ".join(f"*{n}* {label}" for n, label in _BOTTOMS_POSE_MENU_LABELS.items())

_BOTTOMS_QUESTIONS_MESSAGE = (
    "Quick details: reply all in ONE message:\n\n"
    "🧵 *1)* Material & type (e.g. \"Chiffon, Sharara\") — types: Pant / Plazo / "
    "Balloon Salwar / Tulip Salwar / Sharara\n"
    "📏 *2)* Sizes + qty (e.g. \"3 XS, 1 S\"). Unlisted sizes = out of stock\n"
    "💰 *3)* Price (₹)\n"
    "🏷️ *4)* Discount % (or \"none\")\n"
    "⭐ *5)* Bestseller? y/n\n"
    "📸 *6)* Poses: numbers (e.g. \"1,3\"), \"all poses\" for all 5, or a count:\n"
    + _BOTTOMS_POSE_MENU
)

_ALL_POSES_RE = re.compile(r"\ball\s+poses\b", re.I)
_ALL_PREMIUM_RE = re.compile(r"\ball\s+premium\b", re.I)

# The ready-to-post draft (generated images + description, sitting in memory
# waiting for a GO LIVE tap) expires 30 minutes after it's generated
# (2026-09-29: was 10 minutes, unified with the new PROACTIVE expiry below
# so there's one consistent number instead of two different expiry
# behaviours). GO LIVE and Regenerate Description both stop working past
# this and tell the owner it expired (checked reactively, on the next tap —
# see _draft_expired/_reply_expired below); ABORT always keeps working
# regardless. _schedule_draft_expiry below is the PROACTIVE half — fires on
# its own after this same TTL even with zero taps at all, per Part 5 of the
# 2026-09-29 session-gating spec.
DRAFT_TTL_SECONDS = 30 * 60


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
    """The REACTIVE half of draft expiry — fires when the owner taps a
    GO LIVE/regenerate button after DRAFT_TTL_SECONDS has already passed.
    See _schedule_draft_expiry for the PROACTIVE half (fires on its own,
    no tap required)."""
    await query.answer("⏰ This draft has expired — /newproduct to start again.", show_alert=True)
    await query.edit_message_reply_markup(reply_markup=None)
    draft = context.chat_data.get("draft") or {}
    _cancel_expiry_timer(draft)
    await query.message.reply_text(
        f"This draft expired (ready for more than {DRAFT_TTL_SECONDS // 60} minutes) "
        "— /newproduct to start again."
    )
    context.chat_data.clear()


def _cancel_expiry_timer(draft: dict) -> None:
    """Cancels the proactive 30-minute expiry task (see
    _schedule_draft_expiry) if one is running — called wherever a draft's
    session genuinely ends (finalized, aborted, or already reactively
    expired) so the timer doesn't fire a redundant/stale message later."""
    task = draft.pop("expiry_task", None)
    if task is not None and not task.done():
        task.cancel()


def _schedule_draft_expiry(context: ContextTypes.DEFAULT_TYPE, chat_id: int, session_id: str) -> "asyncio.Task":
    """PROACTIVE half of draft expiry (Part 5, 2026-09-29) — unlike
    _reply_expired (which only fires reactively, when the owner taps
    something after time's up), this fires on its own after
    DRAFT_TTL_SECONDS even if the owner never touches the draft again.
    Plain in-process asyncio task: this bot has no job queue/scheduler
    installed and chat_data itself is already in-memory-only (a restart
    wipes the whole draft anyway), so this doesn't add any new
    restart-survival gap — see the discovery note in this task's report.

    Re-checks session_id before acting (same pattern as _live_draft) in
    case this exact draft was already finalized/aborted/replaced by a new
    one in the same chat by the time the timer fires — cancelling the task
    at those points (_cancel_expiry_timer) is the primary guard, this is
    the defensive fallback."""

    async def _fire():
        await asyncio.sleep(DRAFT_TTL_SECONDS)
        draft = context.chat_data.get("draft")
        if draft is None or draft.get("session_id") != session_id:
            return
        context.chat_data.clear()
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏰ This draft expired (no completed posting within "
                    f"{DRAFT_TTL_SECONDS // 60} minutes) — /newproduct to start again."
                ),
            )
        except Exception:
            logger.exception("Failed to send proactive draft-expiry notification")

    return asyncio.create_task(_fire())


def _live_draft(context: ContextTypes.DEFAULT_TYPE, session_id: str) -> dict | None:
    """Fetch the chat's draft only if it's still the SAME session that
    produced the button being tapped. Returns None for a dead/cancelled/
    already-finished session or a stale button from an earlier one — the
    caller is expected to reply accordingly rather than crash or no-op."""
    draft = context.chat_data.get("draft")
    if draft is None or draft.get("session_id") != session_id:
        return None
    return draft


def _flow_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👚 Kurti", callback_data="flow_kurti")],
            [InlineKeyboardButton("👚 Kurti + Pyjama Set", callback_data="flow_kurti_pyjama")],
            [InlineKeyboardButton("🧣 Dupatta", callback_data="flow_dupatta")],
            [InlineKeyboardButton("👖 Women Bottoms", callback_data="flow_bottoms")],
        ]
    )


async def start_new_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/newproduct — entry point (2026-09-29 four-button flow restructure).
    Replaces the old "send any photo to start" trigger: a session now
    always starts by picking one of the 4 category buttons, which decides
    both the photo request sequence and the question set that follow."""
    context.chat_data.clear()
    await update.message.reply_text(
        "What are you listing?", reply_markup=_flow_choice_keyboard()
    )
    return CHOOSING_FLOW


async def confirm_yes_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """"Yes" on the idle confirm prompt (bot/handlers/start.py's
    unrecognized_text, 2026-09-29) — same effect as /newproduct, just
    reached from a button tap on any idle message instead of the command.
    Registered as an ADDITIONAL entry point on this same ConversationHandler
    (see build_conversation_handler below), not a separate flow.

    Records this "Yes" (persisted, survives a restart — bot/state.py) so
    the next 24 hours of idle messages in this chat skip straight past the
    confirm question (see unrecognized_text). A "No" never suppresses
    anything — the confirm question is asked again on the very next idle
    message, per spec."""
    query = update.callback_query
    await query.answer()
    state.record_confirm_yes(update.effective_chat.id)
    context.chat_data.clear()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        "What are you listing?", reply_markup=_flow_choice_keyboard()
    )
    return CHOOSING_FLOW


def _new_draft(context: ContextTypes.DEFAULT_TYPE, listing_type: str) -> dict:
    draft = context.chat_data.setdefault("draft", {})
    draft["session_id"] = _new_session_id()
    draft["listing_type"] = listing_type
    return draft


async def flow_kurti_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _new_draft(context, FLOW_KURTI_ONLY)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Send the front photo of the kurti.")
    return WAITING_FRONT_PHOTO


async def flow_kurti_pyjama_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _new_draft(context, FLOW_KURTI_PYJAMA_SET)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Send the front photo of the kurti.")
    return WAITING_FRONT_PHOTO


async def flow_dupatta_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _new_draft(context, FLOW_DUPATTA)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Send the dupatta photo.")
    return WAITING_DUPATTA_PHOTO


async def flow_bottoms_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _new_draft(context, FLOW_WOMEN_BOTTOMS)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Send the front photo of the bottoms.")
    return WAITING_BOTTOMS_FRONT_PHOTO


# --- Button 1/2: Kurti / Kurti + Pyjama Set (front/back photo, shared) -----


async def receive_front_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.chat_data.get("draft")
    if draft is None:
        return ConversationHandler.END

    largest = update.message.photo[-1]
    file = await context.bot.get_file(largest.file_id)
    draft["front_photo"] = bytes(await file.download_as_bytearray())

    await update.message.reply_text("Got the front photo. Now send the back photo.")
    return WAITING_BACK_PHOTO


async def prompt_for_front_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please send the front photo of the garment first.")
    return WAITING_FRONT_PHOTO


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

    # Which button the owner tapped already decided pyjama-set vs kurti-only
    # (2026-09-29) — no more mid-flow Y/N question here.
    if draft["listing_type"] == FLOW_KURTI_PYJAMA_SET:
        await update.message.reply_text("Now send the pyjama photo.")
        return WAITING_PYJAMA_PHOTO

    await update.message.reply_text(_KURTI_QUESTIONS_MESSAGE, parse_mode="Markdown")
    return WAITING_ANSWERS


async def prompt_for_back_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Got the front photo already — now send the back photo.")
    return WAITING_BACK_PHOTO


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
    await update.message.reply_text(_KURTI_QUESTIONS_MESSAGE, parse_mode="Markdown")
    return WAITING_ANSWERS


async def prompt_for_pyjama_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please send the pyjama photo to continue.")
    return WAITING_PYJAMA_PHOTO


# --- Button 3: Dupatta (single photo) ---------------------------------------


async def receive_dupatta_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.chat_data.get("draft")
    if draft is None:
        return ConversationHandler.END

    draft["active_task"] = asyncio.current_task()
    largest = update.message.photo[-1]
    file = await context.bot.get_file(largest.file_id)
    draft["dupatta_photo"] = bytes(await file.download_as_bytearray())
    draft["raw_photos"] = [draft["dupatta_photo"]]

    await update.message.reply_text("Got the dupatta photo. Looking at it now...")
    draft["color"] = await ai.detect_color(draft["raw_photos"])
    draft.pop("active_task", None)

    await update.message.reply_text(_DUPATTA_QUESTIONS_MESSAGE, parse_mode="Markdown")
    return WAITING_ANSWERS


async def prompt_for_dupatta_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please send the dupatta photo first.")
    return WAITING_DUPATTA_PHOTO


# --- Button 4: Women Bottoms (front/back photo) -----------------------------


async def receive_bottoms_front_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.chat_data.get("draft")
    if draft is None:
        return ConversationHandler.END

    largest = update.message.photo[-1]
    file = await context.bot.get_file(largest.file_id)
    draft["bottoms_front_photo"] = bytes(await file.download_as_bytearray())

    await update.message.reply_text("Got the front photo. Now send the back photo.")
    return WAITING_BOTTOMS_BACK_PHOTO


async def prompt_for_bottoms_front_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please send the front photo of the bottoms first.")
    return WAITING_BOTTOMS_FRONT_PHOTO


async def receive_bottoms_back_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.chat_data.get("draft")
    if draft is None:
        return ConversationHandler.END

    draft["active_task"] = asyncio.current_task()
    largest = update.message.photo[-1]
    file = await context.bot.get_file(largest.file_id)
    draft["bottoms_back_photo"] = bytes(await file.download_as_bytearray())
    draft["raw_photos"] = [draft["bottoms_front_photo"], draft["bottoms_back_photo"]]

    await update.message.reply_text("Got the back photo. Looking at them now...")
    draft["color"] = await ai.detect_color(draft["raw_photos"])
    draft.pop("active_task", None)

    await update.message.reply_text(_BOTTOMS_QUESTIONS_MESSAGE, parse_mode="Markdown")
    return WAITING_ANSWERS


async def prompt_for_bottoms_back_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Got the front photo already — now send the back photo.")
    return WAITING_BOTTOMS_BACK_PHOTO


async def photo_during_answers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Already have the photo(s) for this product — please answer the questions above."
    )
    return WAITING_ANSWERS


async def receive_answers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Dispatches to the right per-flow answer parser based on
    draft["listing_type"] (2026-09-29 four-button flow restructure) — which
    button the owner tapped at the start of the session decided which
    question set they were shown, so it also decides how the reply is
    parsed here."""
    draft = context.chat_data.get("draft")
    if draft is None:
        return ConversationHandler.END

    if draft["listing_type"] in (FLOW_KURTI_ONLY, FLOW_KURTI_PYJAMA_SET):
        return await _receive_kurti_answers(update, context, draft)
    if draft["listing_type"] == FLOW_DUPATTA:
        return await _receive_dupatta_answers(update, context, draft)
    return await _receive_bottoms_answers(update, context, draft)


async def _receive_kurti_answers(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: dict) -> int:
    text = update.message.text.strip()

    try:
        parsed = await ai.parse_kurti_answers(text)
    except Exception:
        logger.exception("Failed to parse new-product answers")
        await update.message.reply_text(
            "Couldn't read that — please reply with all 9 answers in one message."
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
            "Didn't catch a category — reply with one or more of Premium, Kurtis, "
            "Kurti Sets, For Nani/Dadi, For Mom, For Me, On Sale (please resend "
            "all 9 answers)."
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
            "the poses question, and/or premium pose numbers like \"P1, P6\" "
            "for the premium question (please resend all 9 answers)."
        )
        return WAITING_ANSWERS

    if blocked:
        return await _handle_blocked_poses(update.message, draft, selected + premium_selected, blocked)

    return await _generate_and_send_draft(update.message, context, draft, selected + premium_selected)


async def _receive_dupatta_answers(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: dict) -> int:
    text = update.message.text.strip()

    try:
        parsed = await ai.parse_dupatta_answers(text)
    except Exception:
        logger.exception("Failed to parse dupatta answers")
        await update.message.reply_text(
            "Couldn't read that — please reply with all 6 answers in one message."
        )
        return WAITING_ANSWERS

    if _ALL_POSES_RE.search(text):
        parsed["pose_request"] = {"mode": "specific", "pose_numbers": list(prompts.DUPATTA_POSES), "count": 0}

    if parsed["price"] <= 0:
        await update.message.reply_text("Price must be a positive number — please resend all 6 answers.")
        return WAITING_ANSWERS

    if not (0 <= parsed["discount_pct"] < 100):
        await update.message.reply_text("Discount % must be between 0 and 100 — please resend all 6 answers.")
        return WAITING_ANSWERS

    draft["material"] = parsed["material"]
    draft["dupatta_length"] = parsed["length"]
    draft["price"] = parsed["price"]
    draft["discount_pct"] = parsed["discount_pct"]
    draft["categories"] = []  # fixed to the Dupatta collection, no owner choice (2026-09-30)
    draft["is_bestseller"] = parsed["is_bestseller"]
    draft["compare_at_price"] = shopify_client.compute_compare_at_price(
        parsed["price"], parsed["discount_pct"]
    )

    selected = prompts.resolve_dupatta_pose_selection(parsed["pose_request"])
    if not selected:
        await update.message.reply_text(
            "Couldn't resolve any poses from that — reply with pose numbers "
            "like \"1, 3\", \"all poses\", or a count like \"3\" for the "
            "poses question (please resend all 6 answers)."
        )
        return WAITING_ANSWERS

    return await _generate_and_send_draft(update.message, context, draft, selected)


async def _receive_bottoms_answers(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: dict) -> int:
    text = update.message.text.strip()

    try:
        parsed = await ai.parse_bottoms_answers(text)
    except Exception:
        logger.exception("Failed to parse bottoms answers")
        await update.message.reply_text(
            "Couldn't read that — please reply with all 6 answers in one message."
        )
        return WAITING_ANSWERS

    if _ALL_POSES_RE.search(text):
        parsed["pose_request"] = {"mode": "specific", "pose_numbers": list(prompts.BOTTOMS_POSES), "count": 0}

    invalid_sizes = [s["size"] for s in parsed["sizes"] if s["size"] not in VALID_SIZES]
    if invalid_sizes or not parsed["sizes"]:
        await update.message.reply_text(
            f"Not valid sizes: {', '.join(invalid_sizes) or '(none given)'}.\n"
            f"Rama Chikan only sells: {', '.join(VALID_SIZES)}. Please resend all 6 answers."
        )
        return WAITING_ANSWERS

    if parsed["price"] <= 0:
        await update.message.reply_text("Price must be a positive number — please resend all 6 answers.")
        return WAITING_ANSWERS

    if not (0 <= parsed["discount_pct"] < 100):
        await update.message.reply_text("Discount % must be between 0 and 100 — please resend all 6 answers.")
        return WAITING_ANSWERS

    draft["material"] = parsed["material"]
    draft["garment_type"] = parsed["garment_type"]
    draft["size_quantities"] = {s["size"]: s["quantity"] for s in parsed["sizes"]}
    draft["price"] = parsed["price"]
    draft["discount_pct"] = parsed["discount_pct"]
    draft["categories"] = []  # fixed to the Women Bottoms collection, no owner choice
    draft["is_bestseller"] = parsed["is_bestseller"]
    draft["compare_at_price"] = shopify_client.compute_compare_at_price(
        parsed["price"], parsed["discount_pct"]
    )

    selected, blocked = prompts.resolve_bottoms_pose_selection(
        parsed["pose_request"], bool(draft.get("bottoms_back_photo"))
    )
    if not selected and not blocked:
        await update.message.reply_text(
            "Couldn't resolve any poses from that — reply with pose numbers "
            "like \"1, 3\", \"all poses\", or a count like \"3\" for the "
            "poses question (please resend all 6 answers)."
        )
        return WAITING_ANSWERS

    if blocked:
        return await _handle_blocked_poses(update.message, draft, selected, blocked)

    return await _generate_and_send_draft(update.message, context, draft, selected)


async def _handle_blocked_poses(message, draft: dict, selected: list, blocked: list[tuple]) -> int:
    """Shared blocked-pose UI (2026-09-29) — was inlined in receive_answers
    before the four-button restructure split it into three parsers; same
    behaviour, now reusable by the kurti and bottoms paths (dupatta has
    nothing blockable, see resolve_dupatta_pose_selection)."""
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
    await message.reply_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
    return WAITING_ANSWERS


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
        await query.message.reply_text("Nothing to generate — resend your answers with different poses.")
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
    listing_type = draft["listing_type"]
    try:
        if listing_type == FLOW_DUPATTA:
            images, generated_poses, queued_poses = await image_gen.generate_dupatta_images(
                draft["dupatta_photo"], draft["material"], resolved_poses,
            )
        elif listing_type == FLOW_WOMEN_BOTTOMS:
            images, generated_poses, queued_poses = await image_gen.generate_bottoms_images(
                draft.get("bottoms_front_photo"), draft.get("bottoms_back_photo"),
                draft["raw_photos"], draft["garment_type"], draft["material"], resolved_poses,
            )
        else:
            images, generated_poses, queued_poses = await image_gen.generate_model_images(
                draft["raw_photos"], draft.get("front_photo"), draft.get("back_photo"),
                draft.get("pyjama_photo"),
                draft["color"], draft["material"],
                draft["kurti_length"], listing_type, resolved_poses,
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

        copy = await ai.generate_description(draft["color"], draft["material"], listing_type)
        draft["title"] = copy["title"]
        draft["description_html"] = copy["description_html"]
        draft["instagram_caption"] = await ai.generate_instagram_caption(
            images[0], draft["color"], draft["material"], listing_type
        )
        # Starts the reactive 30-minute clock. setdefault, not assignment:
        # this function only ever runs once per draft (the initial
        # generation), but stays defensive against being called twice for
        # the same draft.
        draft.setdefault("ready_at", time.monotonic())
        # Starts the PROACTIVE 30-minute clock (Part 5, 2026-09-29) — only
        # once per draft, same defensiveness as ready_at above.
        if "expiry_task" not in draft:
            draft["expiry_task"] = _schedule_draft_expiry(
                context, message.chat_id, draft["session_id"]
            )
    except image_gen.MissingReferenceError as exc:
        logger.warning("Missing reference photo for pose %s: %s", exc.pose_id, exc)
        await message.reply_text(
            f"Can't generate pose {exc.pose_id} — the {exc.reference_kind.upper()} "
            "reference photo is missing. Send that photo and resend your answers "
            "to retry (nothing else was affected)."
        )
        return WAITING_ANSWERS
    except Exception:
        logger.exception("Image/description generation failed")
        await message.reply_text(
            "Image generation failed — nothing was published. Send your answers again to retry."
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
    price_line = f"₹{draft['price']:.0f}"
    if draft.get("compare_at_price"):
        price_line = f"~₹{draft['compare_at_price']:.0f}~ ₹{draft['price']:.0f}"

    lines = [
        f"*{draft['title']}*",
        "",
        _description_html_to_telegram_text(draft["description_html"]),
        "",
    ]

    listing_type = draft["listing_type"]
    unlisted: list[str] = []
    if listing_type in ("kurti_only", "kurti_pyjama_set"):
        sizes_summary = ", ".join(
            f"{size}: {qty}" for size, qty in draft["size_quantities"].items()
        )
        unlisted = [s for s in VALID_SIZES if s not in draft["size_quantities"]]
        lines += [
            f"Material: {draft['material']}",
            f"Category: {', '.join(draft['categories'])}",
            f"Length: {draft['kurti_length'].capitalize()}",
            "Listing: " + (
                "Kurti + Pyjama Set"
                + (" (real pyjama reference photo used)" if draft.get("pyjama_photo") else "")
                if listing_type == "kurti_pyjama_set"
                else "Kurti Only (bottom shown is styling reference, not included)"
            ),
            "Poses used: " + ", ".join(str(p) for p in draft["generated_poses"]),
            f"Sizes in stock: {sizes_summary}",
        ]
    elif listing_type == "dupatta":
        lines += [
            f"Material: {draft['material']}",
            f"Length: {draft['dupatta_length']}",
            "Listing: Dupatta",
            "Poses used: " + ", ".join(str(p) for p in draft["generated_poses"]),
        ]
    else:  # women_bottoms
        sizes_summary = ", ".join(
            f"{size}: {qty}" for size, qty in draft["size_quantities"].items()
        )
        unlisted = [s for s in VALID_SIZES if s not in draft["size_quantities"]]
        lines += [
            f"Material: {draft['material']}",
            f"Type: {draft['garment_type']}",
            "Listing: Women Bottoms",
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


def _chunk_media(items: list) -> list[list]:
    """Split into chunks of at most _MAX_MEDIA_GROUP_SIZE. Never leaves a
    trailing chunk of exactly 1 item — borrows one back from the previous
    chunk instead, since sendMediaGroup requires at least 2 per call. Works
    on any list (raw (index, image_bytes) pairs, in _send_draft_preview's
    case) — chunking only cares about length, not element type."""
    chunks: list[list] = []
    remaining = list(items)
    while len(remaining) > _MAX_MEDIA_GROUP_SIZE:
        chunks.append(remaining[:_MAX_MEDIA_GROUP_SIZE])
        remaining = remaining[_MAX_MEDIA_GROUP_SIZE:]
    chunks.append(remaining)
    if len(chunks) > 1 and len(chunks[-1]) == 1:
        chunks[-1].insert(0, chunks[-2].pop())
    return chunks


# Added 2026-09-26 after a real production crash: uploading several freshly
# generated images in one sendMediaGroup call hit a mid-upload dropped
# connection (httpx.ReadError -> telegram.error.NetworkError) and the whole
# draft preview was lost — even though every image had already been
# generated at real OpenAI API cost. Retries only a genuine transient
# network failure (NetworkError and its subclass TimedOut); anything else
# (e.g. BadRequest) is a real error and is never retried, since retrying it
# would just repeat the same failure.
_NETWORK_RETRY_MAX_ATTEMPTS = 3
_NETWORK_RETRY_DELAY_SECONDS = 2


async def _send_with_network_retry(send_fn):
    """send_fn: a zero-arg async callable that performs ONE send attempt and
    builds any request payload (e.g. fresh io.BytesIO / InputMediaPhoto
    objects) fresh each call — a failed attempt can leave a byte stream
    partially consumed, so the payload must never be reused across
    retries."""
    last_exc: NetworkError | None = None
    for attempt in range(1, _NETWORK_RETRY_MAX_ATTEMPTS + 1):
        try:
            return await send_fn()
        except NetworkError as exc:
            # python-telegram-bot's BadRequest is (surprisingly) a subclass
            # of NetworkError, but it's a real, non-transient application
            # error (bad payload, chat not found, etc.) — retrying it would
            # just repeat the identical failure, so it's excluded here and
            # re-raised immediately instead of going through the retry loop.
            if isinstance(exc, BadRequest):
                raise
            last_exc = exc
            if attempt < _NETWORK_RETRY_MAX_ATTEMPTS:
                logger.warning(
                    "Transient network error sending to Telegram (attempt %d/%d): %s",
                    attempt, _NETWORK_RETRY_MAX_ATTEMPTS, exc,
                )
                await asyncio.sleep(_NETWORK_RETRY_DELAY_SECONDS)
    raise last_exc


async def _send_draft_preview(message, draft: dict) -> None:
    images = draft["generated_images"]
    if len(images) == 1:
        await _send_with_network_retry(
            lambda: message.reply_photo(photo=io.BytesIO(images[0]))
        )
    else:
        for chunk in _chunk_media(list(enumerate(images))):
            async def _send_chunk(chunk=chunk):
                media = [
                    InputMediaPhoto(io.BytesIO(img), filename=f"angle-{i}.png")
                    for i, img in chunk
                ]
                return await message.reply_media_group(media=media)

            await _send_with_network_retry(_send_chunk)
    await _send_with_network_retry(
        lambda: message.reply_text(
            _draft_caption(draft), reply_markup=_draft_keyboard(draft["session_id"], draft), parse_mode="Markdown"
        )
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


async def _maybe_finalize_session(message, context: ContextTypes.DEFAULT_TYPE, draft: dict) -> int:
    """Part 4 of the 2026-09-29 session-gating spec: once BOTH platforms
    have been ATTEMPTED at least once (success or failure — draft
    "shopify_attempted"/"instagram_attempted", set by go_live_shopify_tap /
    go_live_instagram_tap right after their own attempt resolves, in
    either order), report the final combined status and end the session so
    /newproduct or an idle message can start a fresh one — PTB won't
    re-check entry points while this ConversationHandler still has a
    tracked state for the chat, so ending it here is what actually
    unblocks that, not just a courtesy message.

    Only one platform having been attempted so far -> no-op, returns
    CONFIRMING unchanged (existing independent-buttons behaviour, both
    buttons stay pressable/retryable exactly as before this spec)."""
    if not (draft.get("shopify_attempted") and draft.get("instagram_attempted")):
        return CONFIRMING

    _cancel_expiry_timer(draft)

    shopify_line = (
        f"✅ Shopify: live — {draft.get('shopify_url', '')}"
        if draft.get("shopify_published")
        else "❌ Shopify: did not go live"
    )
    posted_to = [
        label
        for label, key in (("Instagram", "instagram_published"), ("Facebook", "facebook_published"))
        if draft.get(key)
    ]
    social_line = (
        f"✅ Social: posted to {', '.join(posted_to)}"
        if posted_to
        else "❌ Social: nothing posted (Instagram and Facebook both failed)"
    )
    await message.reply_text(
        "Both platforms have been attempted for this product:\n"
        f"{shopify_line}\n{social_line}\n\n"
        "Ready for the next product — /newproduct, or just send a message."
    )
    context.chat_data.clear()
    return ConversationHandler.END


async def go_live_shopify_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """GO LIVE — SHOPIFY (Part 6). Publishes to Shopify exactly as before —
    unchanged logic. Independent of Instagram: does not touch it directly.
    Since 2026-09-29 (Part 4 session-gating spec), marks this platform
    "attempted" either way and checks whether BOTH platforms are now
    attempted — see _maybe_finalize_session."""
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
            size_quantities=draft.get("size_quantities", {}),
            image_resource_urls=image_urls,
            material=draft["material"],
            categories=draft["categories"],
            is_bestseller=draft["is_bestseller"],
            listing_type=draft["listing_type"],
            dupatta_length=draft.get("dupatta_length"),
        )
    except Exception:
        logger.exception("Failed to publish product to Shopify")
        draft["shopify_attempted"] = True
        await query.message.reply_text(
            "Something went wrong publishing this to Shopify — nothing went live. "
            "Tap GO LIVE — SHOPIFY again to retry, or ABORT to discard this draft."
        )
        return await _maybe_finalize_session(query.message, context, draft)

    draft["shopify_published"] = True
    draft["shopify_attempted"] = True
    draft["shopify_url"] = f"https://ramachikan.com/products/{product['handle']}"
    await query.edit_message_reply_markup(reply_markup=_draft_keyboard(session_id, draft))
    await query.message.reply_text(
        f"🟢 Live on Shopify: *{product['title']}*\n{draft['shopify_url']}",
        parse_mode="Markdown",
    )
    return await _maybe_finalize_session(query.message, context, draft)


async def go_live_instagram_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """"Go IG + FB" (Part 6; Facebook added 2026-09-23). Posts ALL generated
    images (as one carousel per platform when there are 2+, max 10 each)
    plus caption/hashtags via upload-post, to Instagram AND Facebook in one
    call. Independent of Shopify: does not touch it directly. The two
    platforms are reported separately — one can succeed while the other
    fails (e.g. the Facebook Page isn't connected in Upload-Post yet) —
    and the button is only marked done (draft["instagram_published"]) if at
    least one platform actually posted, so a fully-failed attempt is always
    retryable without risking a duplicate post on the platform that already
    worked. Since 2026-09-29 (Part 4 session-gating spec), marks this
    platform "attempted" either way and checks whether BOTH platforms are
    now attempted — see _maybe_finalize_session."""
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
        draft["instagram_attempted"] = True
        await query.message.reply_text(
            f"Something went wrong posting — nothing was posted.\n{exc}\n"
            "Tap Go IG + FB again to retry, or ABORT to discard this draft."
        )
        return await _maybe_finalize_session(query.message, context, draft)
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
    draft["instagram_attempted"] = True

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
    return await _maybe_finalize_session(query.message, context, draft)


async def abort_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    session_id = query.data.split(":", 1)[1]
    draft = _live_draft(context, session_id)
    if draft is None:
        await _reply_dead_session(query)
        return ConversationHandler.END

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    _cancel_expiry_timer(draft)
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
        # /newproduct (2026-09-29 four-button flow restructure) replaces the
        # old "send any photo" entry point — a session now always starts by
        # picking one of the 4 category buttons, which decides both the
        # photo sequence and the question set that follow. confirm_yes_tap
        # is the same entry reached via the idle "Do you want to push a
        # product..." Yes button (bot/handlers/start.py's unrecognized_text)
        # instead of the command. The 4 flow_*_tap handlers are ALSO
        # registered here (in addition to inside CHOOSING_FLOW below) so
        # that the 24h "Yes was recent" skip path (unrecognized_text sending
        # the 4-button message directly, without the confirm question) can
        # be tapped straight from idle too, not only from within
        # CHOOSING_FLOW — the two registrations never conflict, since PTB
        # only consults entry_points when this chat has no tracked state.
        entry_points=[
            CommandHandler("newproduct", start_new_product),
            CallbackQueryHandler(confirm_yes_tap, pattern="^start_confirm_yes$"),
            CallbackQueryHandler(flow_kurti_tap, pattern="^flow_kurti$"),
            CallbackQueryHandler(flow_kurti_pyjama_tap, pattern="^flow_kurti_pyjama$"),
            CallbackQueryHandler(flow_dupatta_tap, pattern="^flow_dupatta$"),
            CallbackQueryHandler(flow_bottoms_tap, pattern="^flow_bottoms$"),
        ],
        states={
            CHOOSING_FLOW: [
                CallbackQueryHandler(flow_kurti_tap, pattern="^flow_kurti$"),
                CallbackQueryHandler(flow_kurti_pyjama_tap, pattern="^flow_kurti_pyjama$"),
                CallbackQueryHandler(flow_dupatta_tap, pattern="^flow_dupatta$"),
                CallbackQueryHandler(flow_bottoms_tap, pattern="^flow_bottoms$"),
            ],
            WAITING_FRONT_PHOTO: [
                MessageHandler(filters.PHOTO, receive_front_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_for_front_photo),
            ],
            WAITING_BACK_PHOTO: [
                MessageHandler(filters.PHOTO, receive_back_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_for_back_photo),
            ],
            WAITING_PYJAMA_PHOTO: [
                MessageHandler(filters.PHOTO, receive_pyjama_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_for_pyjama_photo),
            ],
            WAITING_DUPATTA_PHOTO: [
                MessageHandler(filters.PHOTO, receive_dupatta_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_for_dupatta_photo),
            ],
            WAITING_BOTTOMS_FRONT_PHOTO: [
                MessageHandler(filters.PHOTO, receive_bottoms_front_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_for_bottoms_front_photo),
            ],
            WAITING_BOTTOMS_BACK_PHOTO: [
                MessageHandler(filters.PHOTO, receive_bottoms_back_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_for_bottoms_back_photo),
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
