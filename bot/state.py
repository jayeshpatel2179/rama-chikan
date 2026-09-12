"""Tiny on-disk state store for things that must survive a bot restart but
don't need a real database — currently just which background preset was
used last, so background alternation (A -> B -> A -> ...) across products
keeps going correctly after a restart instead of resetting.

Note: the project has a Postgres URL sitting unused in .env
(DATABASE_URL) — if that ever gets wired up for something else, this is a
natural candidate to migrate into it. Not worth standing up a DB connection
for one string value today.
"""

import json
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
