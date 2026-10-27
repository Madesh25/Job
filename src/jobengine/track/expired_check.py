"""Weekly link check (Autopilot I1, 26 Oct): open jobs whose posting closed are marked Expired.

Once a week (from Sunday 09:00 India time, or the first day after when the bot was off) the
bot opens the job link of every open job (Screened, Approved, Resume built) with the same
check as the Approve button (apply/link.py closed_reason: HTTP 404 or 410, a closed-posting
text, a schema.org validThrough in the past, Greenhouse's ?error=true). A job whose Expires
date has passed counts as closed without opening the link. LinkedIn and blocked sites are
never opened. Anything unclear counts as open.

Notion Config `schedule.expired_check` = `false` switches it off (default on).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from urllib.parse import urlsplit

from jobengine.apply import link
from jobengine.screen.models import JobRow

SWITCH_KEY = "schedule.expired_check"
DONE_KEY = "expired.check"  # bot_state: {"date": <local iso date of the last check>}
OPEN_STATUSES = ("Screened", "Approved", "Resume built")
SUNDAY = 6
START = time(9, 0)
MAX_LINKS = 80  # one plain GET each; the rest wait for next week
SHOWN = 15


def is_on(config: Any) -> bool:
    return (config.get(SWITCH_KEY) or "true").strip().lower() != "false"


def due(config: Any, state: Any, now: datetime) -> bool:
    """From Sunday 09:00; a week after the last check when the bot missed a Sunday."""
    if not is_on(config) or now.time() < START:
        return False
    raw = ((state.get(DONE_KEY) if state is not None else None) or {}).get("date")
    try:
        last = date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        last = None
    if last is None:
        return now.weekday() == SUNDAY
    return (now.date() - last).days >= 7


def mark_done(state: Any, day: date) -> None:
    if state is not None:
        state.set(DONE_KEY, {"date": day.isoformat()})


@dataclass
class Result:
    closed: list[tuple[JobRow, str]] = field(default_factory=list)
    checked: int = 0
    not_opened: int = 0  # LinkedIn, a blocked site, or no link
    left: int = 0  # over MAX_LINKS: next week
    total: int = 0  # open jobs looked at

    def text(self) -> str:
        total = self.total
        if not self.closed:
            lines = [f"🧹 Weekly link check: {self.checked} open job link(s) checked, none "
                     "closed."]
        else:
            lines = [f"🧹 Weekly link check: {len(self.closed)} of {total} open jobs are closed "
                     "and now Expired:"]
            lines += [f"- {row.company}, {row.role} ({row.status}): {why}"
                      for row, why in self.closed[:SHOWN]]
            if len(self.closed) > SHOWN:
                lines.append(f"... and {len(self.closed) - SHOWN} more (Notion: Status "
                             "Expired).")
            lines.append("Wrong? Set the Status back in Notion.")
        if self.not_opened:
            lines.append(f"{self.not_opened} not opened (LinkedIn, a blocked site or no link).")
        if self.left:
            lines.append(f"{self.left} wait for next week (at most {MAX_LINKS} links a week).")
        return "\n".join(lines)


def openable(url: str | None) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    return bool(url) and url.lower().startswith("https://") and "linkedin" not in host


def _day(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def check(rows: Iterable[JobRow], get_page: link.PageGetter, today: date,
          limit: int = MAX_LINKS) -> Result:
    """Which rows look closed. A blocked host answers HttpError inside get_page and counts
    as open (closed_reason returns None)."""
    result = Result()
    for row in rows:
        result.total += 1
        ends = _day(row.expires)
        if ends is not None and ends < today:
            result.closed.append((row, f"its Expires date {ends.isoformat()} has passed"))
            continue
        if not openable(row.url):
            result.not_opened += 1
            continue
        if result.checked >= limit:
            result.left += 1
            continue
        result.checked += 1
        why = link.closed_reason(row.url or "", get_page, today)
        if why:
            result.closed.append((row, why))
    return result


def expire(result: Result, update: Callable[[str, dict[str, Any]], None]) -> None:
    for row, _why in result.closed:
        update(row.page_id, {"Status": "Expired"})
