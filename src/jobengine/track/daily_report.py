"""Daily report (Autopilot H1, 25 Oct): what the bot did today, every evening, on Telegram and
by email, even when nothing happened ("0 new jobs today"), so a silent stop shows.

Notion Config `schedule.report` (default true) switches it off; `schedule.report_time`
(default 21:45) and `schedule.report_timezone` (default Asia/Kolkata) say when. It comes
once a day, at that time or within REPORT_WINDOW after it (long polling checks every few
minutes; Cloud Scheduler calls POST /tasks/report). The numbers come from the day ledger
(track/day_ledger.py).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from jobengine.screen import schedule

SWITCH_KEY = "schedule.report"
TIME_KEY = "schedule.report_time"
ZONE_KEY = "schedule.report_timezone"
SENT_KEY = "report.sent"  # bot_state: {"date": <local iso date of the last report>}
DEFAULT_TIME = "21:45"
DEFAULT_ZONE = "Asia/Kolkata"
REPORT_WINDOW = timedelta(hours=3)
VERDICTS = ("Apply high", "Apply normal", "Apply low", "Needs review", "Skip")
SOURCE_NAMES = {"gmail": "Email alerts", "adzuna": "Adzuna", "jooble": "Jooble",
                "nofluffjobs": "NoFluffJobs", "iamexpat": "IamExpat", "ats": "Company sites"}


def is_on(config: Any) -> bool:
    return (config.get(SWITCH_KEY) or "true").strip().lower() != "false"


def report_time(config: Any) -> time:
    return schedule._time(config.get(TIME_KEY), DEFAULT_TIME)


def zone(config: Any) -> ZoneInfo:
    try:
        return ZoneInfo(str(config.get(ZONE_KEY) or DEFAULT_ZONE).strip())
    except (KeyError, ValueError):
        return ZoneInfo(DEFAULT_ZONE)


def local(now: datetime, config: Any) -> datetime:
    return (now if now.tzinfo is not None else now.astimezone()).astimezone(zone(config))


def due(config: Any, state: Any, now: datetime) -> bool:
    """True when today's report has not gone out and its time came within the window."""
    if not is_on(config):
        return False
    here = local(now, config)
    start = datetime.combine(here.date(), report_time(config), tzinfo=here.tzinfo)
    if not start <= here <= start + REPORT_WINDOW:
        return False
    sent = (state.get(SENT_KEY) if state is not None else None) or {}
    return sent.get("date") != here.date().isoformat()


def mark_sent(state: Any, day: date) -> None:
    if state is not None:
        state.set(SENT_KEY, {"date": day.isoformat()})


def _n(entry: Mapping[str, Any], name: str) -> int:
    return int(entry.get(name) or 0)


def fetch_line(entry: Mapping[str, Any]) -> str:
    runs = list(entry.get("sweeps") or [])
    if not runs:
        return "🔎 Fetch: no /fetch ran today."
    totals: dict[str, int] = {}
    for run in runs:
        for name, n in (run.get("sources") or {}).items():
            totals[name] = totals.get(name, 0) + int(n or 0)
    new = sum(int(run.get("new") or 0) for run in runs)
    at = ", ".join(str(run.get("at")) for run in runs)
    found = ", ".join(f"{SOURCE_NAMES.get(k, k)} {v}" for k, v in totals.items() if v)
    return (f"🔎 Fetch: {len(runs)} run(s) ({at}), {new} new job(s) saved."
            + (f" Found: {found}." if found else " No source found a job."))


def screen_line(entry: Mapping[str, Any]) -> str:
    counts = {v: _n(entry, f"screened.{v}") for v in VERDICTS}
    total = sum(counts.values())
    if not total:
        return "🧪 Screening: nothing screened today."
    parts = ", ".join(f"{n} {v.lower()}" for v, n in counts.items() if n)
    return f"🧪 Screening: {total} screened ({parts})."


def resume_line(entry: Mapping[str, Any]) -> str:
    built = _n(entry, "resumes")
    matches: Sequence[Sequence[int]] = entry.get("matches") or []
    if not built:
        return "📄 Resumes: none built today."
    line = f"📄 Resumes: {built} built"
    if matches:
        shown = sum(m[0] for m in matches) / len(matches)
        total = sum(m[1] for m in matches) / len(matches)
        low = min(matches, key=lambda m: m[0] / m[1])
        line += (f", keyword match on average {shown:.0f} of {total:.0f} (lowest {low[0]} of "
                 f"{low[1]})")
    return line + "."


def outreach_line(entry: Mapping[str, Any]) -> str:
    return (f"👥 Contacts found: {_n(entry, 'contacts')}. ✉️ Gmail drafts: {_n(entry, 'drafts')}, "
            f"mails sent: {_n(entry, 'mails_sent')}. 📩 Replies: {_n(entry, 'replies')}.")


def text(entry: Mapping[str, Any], day: date, *, month_cost: str, credits: str,
         tokens: Sequence[str] = (), waiting: Sequence[str] = ()) -> str:
    """The report. `month_cost` is costs.month_line, `credits` the CreditBook line."""
    lines = [f"📊 Daily report, {day:%a %d %b}"]
    if not entry.get("sweeps") and not any(_n(entry, f"screened.{v}") for v in VERDICTS):
        lines.append("Nothing ran today: 0 new jobs, 0 screened. If the schedule is on, "
                     "something stopped it: check /health and send Claude Code the bot log.")
    lines += [fetch_line(entry), screen_line(entry), resume_line(entry), outreach_line(entry),
              f"💰 AI today: about ${float(entry.get('cost') or 0):.2f}. {month_cost}",
              f"💳 {credits}"]
    lines += [f"🔑 {line}" for line in tokens]
    if waiting:
        lines.append("📌 Waiting for you: " + ", ".join(waiting) + ".")
    return "\n".join(lines)
