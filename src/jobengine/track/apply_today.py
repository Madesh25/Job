"""Apply today (S4, 23 Nov, your ask): each morning the 3 to 5 best fresh jobs, best first.

Applying in the first 1 to 2 days after a job is posted gets far more replies than a week
later, so each morning (08:30 India time, after the 07:00 run built the resumes) the bot sends
one card with the jobs that are all three of:
- fresh: posted in the last 48 hours (Posted date; the day /fetch first found it when the
  board gave no date);
- good: Screen verdict Apply high or Apply normal;
- ready: Status "Resume built" (resume done, not applied yet).

Best first: Apply high before Apply normal, then the newest. At most 5; fewer when fewer are
ready, and no card when none is (the 22:00 nudge still lists every ready job). Each line has
its /applypack, and one "I applied" button per job (the same as on the nudge cards).

Notion Config: `schedule.apply_today` (`false` turns it off), `schedule.apply_today_time`
(default 08:30). India time (`schedule.report_timezone`). Holiday mode sends nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from jobengine.screen import schedule
from jobengine.sweep.models import COUNTRY_FLAGS

SWITCH_KEY = "schedule.apply_today"
TIME_KEY = "schedule.apply_today_time"
DEFAULT_TIME = "08:30"
SENT_KEY = "applytoday.sent"  # bot_state: {"day": "2026-11-24"}
WINDOW = timedelta(hours=3)
FRESH = timedelta(hours=48)
MAX_JOBS = 5
READY = "Resume built"
GOOD = ("Apply high", "Apply normal")


def is_on(config: Any) -> bool:
    return (config.get(SWITCH_KEY) or "true").strip().lower() != "false"


def due(config: Any, state: Any, here: datetime) -> bool:
    """From the set time (default 08:30) for 3 hours, once a day."""
    if not is_on(config):
        return False
    at = schedule._time(config.get(TIME_KEY), DEFAULT_TIME)
    start = datetime.combine(here.date(), at, tzinfo=here.tzinfo)
    if not start <= here <= start + WINDOW:
        return False
    sent = (state.get(SENT_KEY) if state is not None else None) or {}
    return sent.get("day") != here.date().isoformat()


def mark_sent(state: Any, day: date) -> None:
    if state is not None:
        state.set(SENT_KEY, {"day": day.isoformat()})


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str((value or {}).get("start") if isinstance(value, dict) else value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def posted(values: Mapping[str, Any]) -> date | None:
    return _day(values.get("Posted date")) or _day(values.get("First seen"))


def pick(rows: Sequence[tuple[str, Mapping[str, Any]]], today: date,
         n: int = MAX_JOBS) -> list[tuple[str, Mapping[str, Any]]]:
    """The fresh, good, ready jobs, best first (Apply high, then the newest)."""
    since = today - timedelta(days=FRESH.days)
    out = []
    for page_id, values in rows:
        day = posted(values)
        if (values.get("Status") == READY and values.get("Screen verdict") in GOOD
                and day is not None and since <= day <= today):
            out.append((page_id, values))
    out.sort(key=lambda r: (GOOD.index(r[1]["Screen verdict"]),
                            -(posted(r[1]) or today).toordinal(), str(r[1].get("Company"))))
    return out[:n]


def age(day: date | None, today: date) -> str:
    days = (today - day).days if day else None
    return {0: "posted today", 1: "posted yesterday"}.get(days, "posted 2 days ago")


def line(number: int, values: Mapping[str, Any], ref: str, today: date) -> str:
    flag = COUNTRY_FLAGS.get(str(values.get("Country") or ""), "")
    place = ", ".join(p for p in (values.get("City"), values.get("Country")) if p)
    return (f"{number}. {flag} {values.get('Company') or '?'}, {values.get('Role') or '?'}"
            f"{f' ({place})' if place else ''}\n"
            f"   {values.get('Screen verdict')} · {age(posted(values), today)} · "
            f"/applypack {ref}").replace(f"{number}.  ", f"{number}. ")


def text(jobs: Sequence[tuple[str, Mapping[str, Any], str]], today: date,
         minutes: int = 10) -> str:
    """The card: `jobs` are (page_id, values, ref), best first."""
    head = (f"☀️ Apply today, {today:%a %d %b}: {len(jobs)} fresh job(s), best first. "
            "Applying in the first 1 to 2 days gets far more replies.")
    body = [line(n, values, ref, today) for n, (_pid, values, ref) in enumerate(jobs, 1)]
    tail = (f"About {minutes} minutes each. /applypack gives the resume, the answers and the "
            "autofill code; tap I applied when it is sent.")
    return "\n\n".join([head, "\n".join(body), tail])


def buttons(jobs: Sequence[tuple[str, Mapping[str, Any], str]]) -> list[tuple[str, str]]:
    return [(f"✅ I applied: {str(values.get('Company') or '?')[:24]}", f"ia:{ref}")
            for _pid, values, ref in jobs]
