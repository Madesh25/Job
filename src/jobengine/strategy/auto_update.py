"""Monthly /update by itself (Autopilot I5, 26 Oct; Friday evening since 5 Nov).

The bot runs the same research as /update once per /fetch gate period and sends the tips
with Adopt / Reject buttons. Nothing else changes: the gate opens only when you decide every
tip, as with /update.

When (5 Nov, the autopilot timetable): on the **Friday evening** (from FRIDAY_START, the
report time zone, India by default) before the gate closes, when it closes within
FRIDAY_WINDOW days, so you review the tips on Saturday (about 20 minutes), the same day as
/newcompanies. The Friday is the first one in that window. If the bot was off then, or the
research failed, it runs from START on each of the last LAST_CHANCE days before the gate
closes (and after it closed), once a day until it succeeds.

Notion Config `strategy.auto_update` (default true) switches it off. It does not run while a
review is still open (an earlier /update with undecided tips).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

SWITCH_KEY = "strategy.auto_update"
DONE_KEY = "strategy.auto"  # bot_state: {"for": <gate date>, "date": <run day>, "ok": bool}
FRIDAY = 4
FRIDAY_START = time(19, 0)  # Friday evening: the tips wait for your Saturday
FRIDAY_WINDOW = 8  # the Friday on which the gate closes in 1 to 8 days
LAST_CHANCE = 2  # missed Friday: from START on each of the last 2 days
DAYS_BEFORE = FRIDAY_WINDOW  # (kept for old imports)
START = time(7, 0)  # after the morning check (06:30), before you start the day
INTRO = "🤖 Monthly strategy research, run by itself"
FAILED = (f"{INTRO}: it did not finish (see below). It tries again in the last days before "
          "/fetch closes; you can also send /update.")


def is_on(config: Any) -> bool:
    return (config.get(SWITCH_KEY) or "true").strip().lower() != "false"


def days_left(updated: date | None, refresh_days: int | None, today: date) -> int | None:
    """Days the /fetch gate stays open (0 or less: closed); None when it was never stamped."""
    if updated is None:
        return None
    return (refresh_days or 30) - (today - updated).days


def due(config: Any, state: Any, updated: date | None, now: datetime,
        review_open: bool) -> bool:
    """True when now is the time to run the research by itself: the Friday evening before
    the gate closes, else (missed or failed) a morning of the last LAST_CHANCE days."""
    if not is_on(config) or review_open:
        return False
    left = days_left(updated, config.get_int("strategy_refresh_days"), now.date())
    if left is None:
        return False
    done = (state.get(DONE_KEY) if state is not None else None) or {}
    if done.get("for") == updated.isoformat() and (
            done.get("ok") or done.get("date") == now.date().isoformat()):
        return False  # done this period, or already tried today
    friday = (now.weekday() == FRIDAY and now.time() >= FRIDAY_START
              and 1 <= left <= FRIDAY_WINDOW)
    last_chance = left <= LAST_CHANCE and now.time() >= START
    return friday or last_chance


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
    return (f"{INTRO} ({when}). Adopt or reject each tip below (Saturday is a good day for "
            "it, about 20 minutes); /fetch opens again for the next month when every tip is "
            "decided.")
