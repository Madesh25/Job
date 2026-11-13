"""Weekly link check (Autopilot I1, 26 Oct): open jobs whose posting closed are marked Expired.

Once a week (from Sunday 09:00 India time, or the first day after when the bot was off) the
bot opens the job link of every open job (Screened, Approved, Resume built) with the same
check as the Approve button (apply/link.py closed_reason: HTTP 404 or 410, a closed-posting
text, a schema.org validThrough in the past, Greenhouse's ?error=true). A job whose Expires
date has passed counts as closed without opening the link. LinkedIn and blocked sites are
never opened. Anything unclear counts as open.

Notion Config `schedule.expired_check` = `false` switches it off (default on).

10 Nov (your request): the same check also runs in every /fetch (`after_fetch`, config
sweep.link_check), so a closed job leaves /pending the same day: the jobs you will see
first, at most `max_links` a /fetch, each job again after `recheck_days`. A LinkedIn job,
which the bot never opens, counts as closed `linkedin_days` after it was posted.
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


# 13 Nov /fetch: about 50 of the 150 links checked were sites that always refuse the bot
# (justjoin.it, pracuj.pl, IrishJobs, theprotocol.it, Glassdoor) or Adzuna's own link pages
# (HTTP 403): never a real check, and they took the place of links that can be checked.
NEVER_OPENED = ("adzuna.",)


def openable(url: str | None, blocked: Iterable[str] = ()) -> bool:
    """A link the check can read: https, not LinkedIn, not a site that refuses the bot."""
    host = (urlsplit(url or "").hostname or "").lower()
    if not url or not url.lower().startswith("https://") or "linkedin" in host:
        return False
    if any(part in host for part in NEVER_OPENED):
        return False
    return not any(host == h or host.endswith("." + h) for h in blocked)


def _day(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def linkedin_closed(row: JobRow, today: date, days: int | None) -> str | None:
    """A LinkedIn job posted more than `days` ago (LinkedIn postings close by then)."""
    host = (urlsplit(row.url or "").hostname or "").lower()
    posted = _day(row.posted_date) or _day(row.swept_date)
    if not days or "linkedin" not in host or posted is None:
        return None
    age = (today - posted).days
    return (f"a LinkedIn job posted {age} days ago (LinkedIn postings close after {days})"
            if age > days else None)


def check(rows: Iterable[JobRow], get_page: link.PageGetter, today: date,
          limit: int = MAX_LINKS, linkedin_days: int | None = None) -> Result:
    """Which rows look closed. A blocked host answers HttpError inside get_page and counts
    as open (closed_reason returns None)."""
    result = Result()
    for row in rows:
        result.total += 1
        ends = _day(row.expires)
        if ends is not None and ends < today:
            result.closed.append((row, f"its Expires date {ends.isoformat()} has passed"))
            continue
        old = linkedin_closed(row, today, linkedin_days)
        if old:
            result.closed.append((row, old))
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


def expire(result: Result, update: Callable[[str, dict[str, Any]], None],
           today: Any = None) -> None:
    for row, _why in result.closed:
        update(row.page_id, {"Status": "Expired", "Skip reason": "Expired",
                             **({"Last activity date": today} if today else {})})


# ---------------------------------------------------------------- in every /fetch (10 Nov)

FETCH_STATUSES = ("Screened", "Approved", "Resume built", "New")
FETCH_KEY = "expired.fetch"  # bot_state: {page_id: iso date of its last check}
DEFAULTS = {"enabled": True, "max_links": 150, "recheck_days": 3, "linkedin_days": 30,
            "workers": 5}


def fetch_settings(sweep: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **(sweep.get("link_check") or {})}


def _order(row: JobRow) -> tuple[int, int]:
    """/pending first (what you see next), then your approved jobs, then the rest."""
    rank = {"Screened": 0, "Approved": 1, "Resume built": 1}.get(row.status or "", 2)
    if row.status == "Screened" and row.screen_verdict not in ("Apply", "Needs review"):
        rank = 2
    newest = _day(row.posted_date) or _day(row.swept_date) or date.min
    return rank, -newest.toordinal()  # newest first in each group


def after_fetch(rows: Iterable[JobRow], get_page: link.PageGetter, today: date,
                state: Any, cfg: dict[str, Any],
                update: Callable[[str, dict[str, Any]], None]) -> Result:
    """The link check of one /fetch: closed jobs become Expired and leave /pending. Each job
    is checked again after `recheck_days`; LinkedIn jobs by their age only."""
    from datetime import timedelta

    from jobengine.parallel import run_all

    saved = dict((state.get(FETCH_KEY) if state is not None else None) or {})
    cutoff = (today - timedelta(days=int(cfg["recheck_days"]))).isoformat()
    saved = {k: v for k, v in saved.items() if str(v) > cutoff}
    # 10 Nov (3-08): 6 of the 10 named were jobs screened Skip, which /pending never shows;
    # their checks are better spent on the jobs you will see.
    rows = sorted((r for r in rows if r.page_id not in saved
                   and (r.screen_verdict or "") != "Skip"), key=_order)
    days = int(cfg["linkedin_days"] or 0) or None
    result = Result()
    to_open: list[JobRow] = []
    for row in rows:
        result.total += 1
        ends = _day(row.expires)
        why = (f"its Expires date {ends.isoformat()} has passed"
               if ends is not None and ends < today else linkedin_closed(row, today, days))
        if why:
            result.closed.append((row, why))
        elif not openable(row.url, cfg.get("blocked_hosts") or ()):
            result.not_opened += 1
            saved[row.page_id] = today.isoformat()
        elif len(to_open) < int(cfg["max_links"]):
            to_open.append(row)
        else:
            result.left += 1
    reasons = run_all(lambda row: link.closed_reason(row.url or "", get_page, today), to_open,
                      max(1, int(cfg["workers"])))
    for row, why in zip(to_open, reasons, strict=True):
        result.checked += 1
        saved[row.page_id] = today.isoformat()
        if why:
            result.closed.append((row, why))
    expire(result, update, today)
    if state is not None:
        state.set(FETCH_KEY, saved)
    return result
