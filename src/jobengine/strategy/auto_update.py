"""Monthly /update by itself (Autopilot I5, 26 Oct).

When the /fetch gate closes within DAYS_BEFORE days (or is already closed), the bot runs the
same research as /update once, from the morning on, and sends the tips with Adopt / Reject
buttons. Nothing else changes: the gate opens only when you decide every tip, as with /update.

Notion Config `strategy.auto_update` (default true) switches it off. It does not run while a
review is still open (an earlier /update with undecided tips) and runs once per gate period;
when the research fails it tries again the next day.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

SWITCH_KEY = "strategy.auto_update"
DONE_KEY = "strategy.auto"  # bot_state: {"for": <gate date>, "date": <run day>, "ok": bool}
DAYS_BEFORE = 5
START = time(7, 0)  # after the morning check (06:30), before you start the day
INTRO = "🤖 Monthly strategy research, run by itself"
FAILED = (f"{INTRO}: it did not finish (see below). It tries again tomorrow; you can also "
          "send /update.")


def is_on(config: Any) -> bool:
    return (config.get(SWITCH_KEY) or "true").strip().lower() != "false"


def days_left(updated: date | None, refresh_days: int | None, today: date) -> int | None:
    """Days the /fetch gate stays open (0 or less: closed); None when it was never stamped."""
    if updated is None:
        return None
    return (refresh_days or 30) - (today - updated).days


def due(config: Any, state: Any, updated: date | None, now: datetime,
        review_open: bool) -> bool:
    """True when today is the day to run the research by itself."""
    if not is_on(config) or review_open or now.time() < START:
        return False
    left = days_left(updated, config.get_int("strategy_refresh_days"), now.date())
    if left is None or left > DAYS_BEFORE:
        return False
    done = (state.get(DONE_KEY) if state is not None else None) or {}
    if done.get("for") != updated.isoformat():
        return True
    return not done.get("ok") and done.get("date") != now.date().isoformat()


def mark(state: Any, updated: date, today: date, ok: bool) -> None:
    if state is not None:
        state.set(DONE_KEY, {"for": updated.isoformat(), "date": today.isoformat(), "ok": ok})


def intro(left: int) -> str:
    if left > 0:
        when = f"/fetch closes in {left} day{'s' if left != 1 else ''}"
    elif left == 0:
        when = "/fetch closes today"
    else:
        when = "/fetch is blocked"
    return (f"{INTRO} ({when}). Adopt or reject each tip below; /fetch opens again for the "
            "next month when every tip is decided.")
