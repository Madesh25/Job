"""Daily report (Autopilot H1, 25 Oct): what the bot did today, every evening, on Telegram and
by email, even when nothing happened ("0 new jobs today"), so a silent stop shows.

Notion Config `schedule.report` (default true) switches it off; `schedule.report_time`
(default 21:45) and `schedule.report_timezone` (default Asia/Kolkata) say when. It comes
once a day, at that time or within REPORT_WINDOW after it (long polling checks every few
minutes; Cloud Scheduler calls POST /tasks/report). The numbers come from the day ledger
(track/day_ledger.py).

Warnings (Autopilot H2) come first, each with ⚠️, when today looks wrong next to the last 7
days: a source that found nothing while it usually finds jobs, a skip rate far above normal,
a resume keyword match far below normal, a Gmail token that stops within 2 days, a contact
provider with no credit left, a scheduled run that did not happen.
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
    totals = _sources(entry)
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


HISTORY_DAYS = 7
SOURCE_USUAL = 3  # a source finding at least this many a day on average is expected today
MIN_SCREENED = 5  # skip rate compared only with this many screened today
SKIP_JUMP = 0.25  # today's skip share this much above the week's: a warning
MATCH_DROP = 0.15  # today's keyword match share this much below the week's: a warning
TOKEN_WARN_DAYS = 2


def _sources(entry: Mapping[str, Any]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for run in entry.get("sweeps") or []:
        for name, n in (run.get("sources") or {}).items():
            totals[name] = totals.get(name, 0) + int(n or 0)
    return totals


def _skip_share(entries: Sequence[Mapping[str, Any]]) -> tuple[float, int]:
    total = sum(_n(e, f"screened.{v}") for e in entries for v in VERDICTS)
    skips = sum(_n(e, "screened.Skip") for e in entries)
    return (skips / total if total else 0.0), total


def _match_share(entries: Sequence[Mapping[str, Any]]) -> tuple[float, int]:
    matches = [m for e in entries for m in (e.get("matches") or []) if m and m[1]]
    if not matches:
        return 0.0, 0
    return sum(m[0] / m[1] for m in matches) / len(matches), len(matches)


def warnings(entry: Mapping[str, Any], history: Sequence[Mapping[str, Any]], *,
             tokens: Sequence[tuple[str, int]] = (), used_up: Sequence[str] = (),
             missed: Sequence[str] = ()) -> list[str]:
    """The ⚠️ lines: today against the last days (`history`, newest first)."""
    out = [f"⚠️ The {at} autopilot did not run today: check /health and the bot log."
           for at in missed]
    swept = [e for e in history if e.get("sweeps")]
    if entry.get("sweeps") and swept:
        today = _sources(entry)
        week: dict[str, int] = {}
        for e in swept:
            for name, n in _sources(e).items():
                week[name] = week.get(name, 0) + n
        for name, total in week.items():
            if total / len(swept) >= SOURCE_USUAL and not today.get(name):
                out.append(f"⚠️ {SOURCE_NAMES.get(name, name)} found 0 jobs today (usually "
                           f"about {round(total / len(swept))} a day): check /sources.")
    skip, screened = _skip_share([entry])
    usual_skip, usual_screened = _skip_share(history)
    if screened >= MIN_SCREENED and usual_screened >= 2 * MIN_SCREENED \
            and skip - usual_skip >= SKIP_JUMP:
        out.append(f"⚠️ Skip rate {skip:.0%} today (usually {usual_skip:.0%}): check the "
                   "skipped jobs in Notion (a rule or a source may be off).")
    match, built = _match_share([entry])
    usual_match, usual_built = _match_share(history)
    if built and usual_built >= 3 and usual_match - match >= MATCH_DROP:
        out.append(f"⚠️ Resume keyword match {match:.0%} today (usually {usual_match:.0%}): "
                   "look at one of today's resumes.")
    for account, left in tokens:
        if left < 0:
            out.append(f"⚠️ Gmail token {account} has expired: send /gmailtoken {account}.")
        elif left <= TOKEN_WARN_DAYS:
            out.append(f"⚠️ Gmail token {account} stops in {left} day(s): send /gmailtoken "
                       f"{account}.")
    if used_up:
        out.append(f"⚠️ No contact credits left this month: {', '.join(used_up)}.")
    return out


def text(entry: Mapping[str, Any], day: date, *, month_cost: str, credits: str,
         tokens: Sequence[str] = (), waiting: Sequence[str] = (),
         warned: Sequence[str] = ()) -> str:
    """The report. `month_cost` is costs.month_line, `credits` the CreditBook line,
    `warned` the warnings() lines (shown first)."""
    lines = [f"📊 Daily report, {day:%a %d %b}", *warned]
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
