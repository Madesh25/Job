"""Saved jobs whose page gives no description (8 Oct, Phase 6).

LinkedIn is never read, so its Unscreened rows always wait for /jd. Other sites may block the
bot too: Pracuj.pl, IrishJobs and Jobs.ie answer HTTP 403, so their alert jobs are saved
without a description. Your decision (8 Oct): they wait for /jd like LinkedIn jobs.

This bot_state entry remembers those rows ({page id: a day}), written by the sweep's backfill
(a page that gave nothing: the day it was tried) and by /screen (a row with no description:
the day first seen).
/jd lists LinkedIn rows plus the noted rows that are still Unscreened, and the backfill does
not read a noted row again for RETRY_DAYS, so the same blocked pages are not opened on every
/fetch.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

KEY = "jd_waiting"
RETRY_DAYS = 7
KEEP_DAYS = 45
MAX_ROWS = 500


def noted(state: Any) -> dict[str, str]:
    """Page id -> ISO day noted. {} without a state."""
    value = state.get(KEY) if state is not None else None
    rows = (value or {}).get("rows")
    return {str(k): str(v) for k, v in rows.items()} if isinstance(rows, dict) else {}


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def note(state: Any, page_ids: Iterable[str], today: date, *, tried: bool = False) -> None:
    """Remember these rows as having no description. `tried` (the backfill read the page):
    the day becomes today; otherwise the first day noted is kept."""
    if state is None:
        return
    rows = noted(state)
    before = dict(rows)
    for page_id in page_ids:
        if tried:
            rows[page_id] = today.isoformat()
        else:
            rows.setdefault(page_id, today.isoformat())
    first = today - timedelta(days=KEEP_DAYS)
    rows = {k: v for k, v in rows.items() if (_day(v) or today) >= first}
    if len(rows) > MAX_ROWS:
        rows = dict(sorted(rows.items(), key=lambda kv: kv[1])[-MAX_ROWS:])
    if rows != before:
        state.set(KEY, {"rows": rows})


def forget(state: Any, page_ids: Iterable[str]) -> None:
    """The rows got a description: they no longer wait."""
    if state is None:
        return
    rows = noted(state)
    gone = [p for p in page_ids if p in rows]
    if gone:
        state.set(KEY, {"rows": {k: v for k, v in rows.items() if k not in gone}})


def recently_tried(state: Any, today: date, days: int = RETRY_DAYS) -> set[str]:
    """Rows noted less than `days` days ago: the backfill leaves them alone."""
    first = today - timedelta(days=days)
    return {k for k, v in noted(state).items() if (_day(v) or today) > first}
