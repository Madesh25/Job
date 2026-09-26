"""Daily screening limit: at most `screening.daily_limit` jobs go to the LLM per day.

The count lives in Bot State so every run that day (CLI, /fetch, /screen, the daily job)
shares it: bot_state["screen.day"] = {"date": "2026-09-27", "count": 12}.
Rows without a description never reach the LLM and do not count.
"""

from __future__ import annotations

from datetime import date

from jobengine.bot_state import BotState
from jobengine.settings import Settings

KEY = "screen.day"
DEFAULT_DAILY_LIMIT = 30


def daily_limit(s: Settings) -> int:
    return max(0, int(s.screening.get("daily_limit", DEFAULT_DAILY_LIMIT)))


def used_today(state: BotState | None, today: date) -> int:
    value = state.get(KEY) if state else None
    if not value or value.get("date") != today.isoformat():
        return 0
    return int(value.get("count") or 0)


def record(state: BotState | None, today: date, count: int) -> None:
    if state is not None:
        state.set(KEY, {"date": today.isoformat(), "count": count})
