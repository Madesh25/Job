"""The /jd backlog (S5, 23 Nov, your ask): about 290 older Unscreened jobs and the LinkedIn
jobs waited for a description, and good new jobs were lost in the pile.

Two rules:
- /jd starts with today's top 5: the 5 jobs of the last 3 days that wait for a description,
  best /fetch match first ("⭐ Top 5" walks them one at a time). The board buttons stay.
- A job never screened (Unscreened, Status New) whose posting (else the day /fetch found it)
  is more than 7 days old is set Expired (Skip reason Expired): too old to apply to, so it
  no longer waits for /jd and /screen no longer takes it. Once a day, at most 100 a run (the
  first clean-up of the old pile takes a few runs); a message says how many.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any

from jobengine.screen.models import JobRow

TOP = 5
TOP_BOARD = "__top__"  # the /jd "⭐ Top 5" button
DAYS = 7
MAX_PER_RUN = 100
KEY = "backlog.expired"  # bot_state: {"day": "2026-11-24"}: the day the pile was cleared
OPEN = (None, "", "New")


def day_of(row: JobRow) -> date | None:
    for day in (row.posted_date, row.swept_date, row.first_seen):
        if isinstance(day, date):
            return day
    return None


def too_old(row: JobRow, today: date) -> bool:
    day = day_of(row)
    return day is not None and day <= today - timedelta(days=DAYS)


def to_expire(rows: Sequence[JobRow], today: date) -> list[JobRow]:
    """Never screened, still New, more than DAYS days old: oldest first."""
    old = [r for r in rows if r.status in OPEN and too_old(r, today)]
    return sorted(old, key=lambda r: day_of(r) or date.min)


def due(state: Any, today: date) -> bool:
    return ((state.get(KEY) if state is not None else None) or {}).get("day") \
        != today.isoformat()


def expire(rows: Sequence[JobRow], update: Callable[[str, dict[str, Any]], None],
           today: date, state: Any = None) -> tuple[int, int]:
    """Expire at most MAX_PER_RUN of `rows`; (expired now, still left). The day is marked
    done once nothing is left, so the next call the same day goes on with the rest."""
    batch = list(rows)[:MAX_PER_RUN]
    for row in batch:
        update(row.page_id, {"Status": "Expired", "Skip reason": "Expired",
                             "Last activity date": today})
    left = max(0, len(rows) - len(batch))
    if not left and state is not None:
        state.set(KEY, {"day": today.isoformat()})
    return len(batch), left


def text(done: int, left: int, waiting: int) -> str:
    more = f" {left} more go in the next runs today." if left else ""
    return (f"🧹 Old jobs cleared: {done} never screened job(s) more than {DAYS} days old are "
            f"set Expired (too old to apply to).{more} {waiting} job(s) of the last 3 days "
            f"wait for a description: /jd shows today's top {TOP}.")


def top_lines(rows: Sequence[JobRow]) -> list[str]:
    out = []
    for n, row in enumerate(rows[:TOP], 1):
        day = row.posted_date or row.swept_date
        when = f", posted {day:%d %b}" if isinstance(day, date) else ""
        out.append(f"{n}. {row.company}, {row.role} ({row.board or 'Other'}{when})")
    return out
