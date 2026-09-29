"""Tiny on-disk state store for things that must survive a bot restart but
don't need a real database — background-preset rotation, and (2026-09-29)
the per-chat "last confirmed Yes" timestamp for the idle push-confirm gate
(bot/handlers/start.py), which explicitly needs to survive a Railway
restart/redeploy since its window is 24 hours.

Note: the project has a Postgres URL sitting unused in .env
(DATABASE_URL) — if that ever gets wired up for something else, this is a
natural candidate to migrate into it. Not worth standing up a DB connection
for a handful of small values today.
"""

import json
import time
from pathlib import Path

_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "agent_state.json"


def _load() -> dict:
    if _STATE_FILE.exists():
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    return {}


def _save(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def next_background_preset() -> str:
    """Call once per product. Alternates 'A' / 'B', persisted across
    restarts. First call ever (no state file yet) starts at 'A'."""
    state = _load()
    last = state.get("last_background_preset")
    next_preset = "B" if last == "A" else "A"
    state["last_background_preset"] = next_preset
    _save(state)
    return next_preset


def next_premium_background_set() -> str:
    """Call once per product, only when that product actually generates at
    least one premium editorial pose (bot/prompts.py PREMIUM_BACKGROUND_SETS).
    Rotates 'A' -> 'B' -> 'C' -> 'A' across DIFFERENT products, persisted
    across restarts — a separate rotation and a separate state key from
    next_background_preset() above, since the premium sets are a distinct
    lettered namespace (3 sets, not 2) used only for the 8 premium poses."""
    state = _load()
    order = ["A", "B", "C"]
    last = state.get("last_premium_background_set")
    next_set = order[(order.index(last) + 1) % len(order)] if last in order else "A"
    state["last_premium_background_set"] = next_set
    _save(state)
    return next_set


# --- Idle push-confirm gate, "once per 24 hours" (2026-09-29) --------------
# Keyed by chat_id (a string, since JSON object keys are always strings) —
# matches this bot's existing per-CHAT (not per-Telegram-user) session
# model (see new_product.py's per_user=False comment: Telegram's "send
# anonymously" group option makes per-user identity unreliable here anyway).

YES_GATE_WINDOW_SECONDS = 24 * 60 * 60


def record_confirm_yes(chat_id: int) -> None:
    """Call exactly when the owner taps "Yes" on the idle push-confirm
    prompt — never on the 24h-skip path itself, which only reads this."""
    state = _load()
    yes_at = state.setdefault("last_confirm_yes_at", {})
    yes_at[str(chat_id)] = time.time()
    _save(state)


def confirm_yes_is_fresh(chat_id: int) -> bool:
    """True if this chat answered "Yes" within the last 24 hours — the
    idle confirm prompt should be skipped (go straight to the 4 category
    buttons) whenever this is True."""
    state = _load()
    yes_at = state.get("last_confirm_yes_at", {}).get(str(chat_id))
    if yes_at is None:
        return False
    return (time.time() - yes_at) < YES_GATE_WINDOW_SECONDS
