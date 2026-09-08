from telegram import Update
from telegram.ext import ContextTypes


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Rama Chikan store agent is online.\n\n"
        "• New product: send the raw garment photo(s) (1 is fine, or a few angles).\n"
        "• Out of stock: send the product slug or product URL.\n\n"
        "Tip: /tips has photo advice for tonal or subtly-embroidered garments."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "New product: send raw photo(s) of the garment (front is enough, more "
        "angles help), tap \"That's all the photos\" when done, then answer "
        "the 11 questions I ask in one message.\n\n"
        "Out of stock: send the product slug or full product URL, confirm "
        "it's the right one, then tell me which size(s) — or say "
        '"mark whole product out of stock".\n\n'
        "/tips — photography advice for garments the AI struggles with."
    )


async def tips_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Added alongside the tonal-embroidery contrast fix (bot/ai.py's
    detect_embroidery_contrast) — the bot warns automatically when it spots
    a tricky garment, but the advice is worth having on-demand too."""
    await update.effective_message.reply_text(
        "📸 Photography tips for a more accurate product photo:\n\n"
        "• Light from the SIDE, not straight overhead — overhead light "
        "flattens embroidery texture, especially on tonal/self-coloured "
        "thread (embroidery close in colour to the fabric).\n"
        "• Use a plain contrasting background, not white tile — a "
        "coloured sheet or wall shows the garment's true shape and colour "
        "better than a bright white floor.\n"
        "• For tonal or subtle embroidery, send extra close-ups: neckline, "
        "back yoke, and sleeve. Up to 6 photos are accepted, in this "
        "order — front, back, then close-ups.\n"
        "• Lay the garment flat and smooth, or hang it — creases and "
        "shadows can hide the real motif layout."
    )


async def unrecognized_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registered in a later handler group (see bot/main.py) so it only ever
    fires when nothing else claimed the update — i.e. no session is active
    for this chat and the text isn't a product slug/URL either. Covers
    plain messages like "hi" that would otherwise get silently dropped."""
    await update.effective_message.reply_text(
        "Send the product photo(s) to start a new listing, or the product "
        "slug/URL to manage stock. /help for details."
    )
