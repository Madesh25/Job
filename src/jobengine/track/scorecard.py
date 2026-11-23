"""Daily scorecard (S1, 23 Nov, your ask): one line a day that says what the bot did, so a
day of testing after go-live is reading one line instead of every message.

The numbers come from the day ledger (track/day_ledger.py): jobs found by /fetch, screened,
resumes built, forms filled, mails sent, replies, and the runs that failed or logged an
error (Run Log Result Failed or Problems), named with their time. The line opens the
evening daily report; the same numbers are one row a day in Notion Daily Scorecard,
updated after every run. A scorecard problem is only logged: it never stops a run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from jobengine.track import day_ledger

log = logging.getLogger("jobengine.track")

_bound: tuple[Any, Any, Callable[[], datetime]] | None = None  # table, state, now


def bind(table: Any, state: Any, now: Callable[[], datetime]) -> None:
    """Write today's row into `table` after every run from now on (None stops it)."""
    global _bound
    _bound = (table, state, now) if table is not None and state is not None else None


def numbers(entry: Mapping[str, Any]) -> dict[str, int]:
    """Today's numbers from a day ledger entry."""
    def n(name: str) -> int:
        return int(float(entry.get(name) or 0))

    return {
        "Found": sum(int(run.get("new") or 0) for run in entry.get("sweeps") or []),
        "Screened": sum(int(float(v or 0)) for k, v in entry.items()
                        if k.startswith("screened.")),
        "Resumes": n("resumes"), "Forms": n("forms"), "Mails": n("mails_sent"),
        "Replies": n("replies"), "Problems": n("problems"),
    }


def failed(entry: Mapping[str, Any]) -> list[str]:
    """The runs that failed or logged an error today ("Link check 10:15")."""
    return [str(name) for name in entry.get("failed") or []]


def line(entry: Mapping[str, Any]) -> str:
    """The Telegram line: "📊 Today: Found 19 · Screened 11 · ... · Problems 0", and a second
    line with ❌ naming the steps that failed."""
    nums = numbers(entry)
    text = "\U0001F4CA Today: " + " · ".join(f"{k} {v}" for k, v in nums.items())
    names = failed(entry)
    if names:
        text += "\n❌ Failed or with errors: " + ", ".join(names) + " (Run Log has why)"
    return text


def row(entry: Mapping[str, Any], day: date, now: datetime) -> dict[str, Any]:
    names = failed(entry)
    return {
        "Day": day.isoformat(), "Date": day, **numbers(entry),
        "Result": "Problems" if names else "OK", "Failed steps": ", ".join(names),
        "AI cost": round(float(entry.get("cost") or 0), 4), "Updated": now,
    }


def refresh() -> None:
    """Today's row in Notion Daily Scorecard, created or updated (after every run)."""
    if _bound is None:
        return
    table, state, now = _bound
    try:
        moment = now()
        today = moment.date()
        values = row(day_ledger.day(state, today), today, moment)
        day_key = values.pop("Day")
        table.upsert({"Day": day_key}, values)
    except Exception as exc:  # the scorecard never fails the run it counts
        log.info("daily scorecard not written: %s", exc)
