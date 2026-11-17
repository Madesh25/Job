"""The day ledger (Autopilot H1, 25 Oct): what the bot did each day, for the daily report and
the warnings that compare today with the last days.

Each step adds its numbers for today while it runs: every /fetch (its sources and new jobs),
every screened row (by verdict), every resume built and its keyword match, Gmail drafts,
mails sent, replies, contacts found and the AI cost. The bot binds the ledger to its Bot State
once at start (like the AI cost sink); without that (tests, the CLI) nothing is recorded.
Bot State `day.ledger` keeps the last KEEP_DAYS days. A ledger problem is only logged: it
never stops the work it counts.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from jobengine.bot_state import BotState

log = logging.getLogger("jobengine.track")

KEY = "day.ledger"  # bot_state: {"days": {"2026-10-25": {...}}}
KEEP_DAYS = 8
_lock = threading.Lock()  # screening writes rows on several threads
_bound: tuple[BotState, Callable[[], date], Callable[[], datetime]] | None = None


def bind(state: BotState | None, today: Callable[[], date],
         now: Callable[[], datetime] = datetime.now) -> None:
    """Record into this Bot State from now on (None stops recording)."""
    global _bound
    _bound = (state, today, now) if state is not None else None


def _update(state: BotState, day: date, change: Callable[[dict[str, Any]], None]) -> None:
    try:
        with _lock:
            value = state.get(KEY) or {}
            days = dict(value.get("days") or {})
            entry = dict(days.get(day.isoformat()) or {})
            change(entry)
            days[day.isoformat()] = entry
            oldest = (day - timedelta(days=KEEP_DAYS - 1)).isoformat()
            state.set(KEY, {"days": {k: v for k, v in sorted(days.items()) if k >= oldest}})
    except Exception:  # never fail the work over the ledger
        log.exception("could not update the day ledger")


def _bump(entry: dict[str, Any], name: str, n: float) -> None:
    entry[name] = round(float(entry.get(name) or 0) + n, 6)


def count(name: str, n: float = 1) -> None:
    """Add n to today's counter `name` ("resumes", "drafts", "screened.Skip" ...)."""
    if _bound is None or not n:
        return
    state, today, _now = _bound
    _update(state, today(), lambda entry: _bump(entry, name, n))


def add_cost(state: BotState, day: date, dollars: float) -> None:
    _update(state, day, lambda entry: _bump(entry, "cost", dollars))


def add_sweep(sources: Mapping[str, int], new: int, updated: int) -> None:
    """One /fetch: the time it ended, its postings per source and the jobs it saved."""
    if _bound is None:
        return
    state, today, now = _bound
    at = now().strftime("%H:%M")

    def change(entry: dict[str, Any]) -> None:
        runs = list(entry.get("sweeps") or [])
        runs.append({"at": at, "sources": dict(sources), "new": new, "updated": updated})
        entry["sweeps"] = runs

    _update(state, today(), change)


def add_match(shown: int, total: int) -> None:
    """A built resume's keyword match ("shows 9 of 12 job keywords you have")."""
    if _bound is None or total <= 0:
        return
    state, today, _now = _bound

    def change(entry: dict[str, Any]) -> None:
        entry["matches"] = [*(entry.get("matches") or []), [shown, total]]

    _update(state, today(), change)


def day(state: BotState | None, when: date) -> dict[str, Any]:
    """The ledger entry of one day ({} when nothing was recorded)."""
    value = (state.get(KEY) if state is not None else None) or {}
    return dict((value.get("days") or {}).get(when.isoformat()) or {})
