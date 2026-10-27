"""Morning check (Autopilot C, 26 Oct): one message before the morning run, with what would
stop the autopilot first and how to fix it.

Notion Config `schedule.health` (default true) switches it off; `schedule.health_time`
(default 06:30, India time like the daily report) says when. It comes once a day, at that
time or within WINDOW after it (long polling checks every few minutes; Cloud Scheduler calls
POST /tasks/health).

Three parts:
- ⛔ Blocks the run: a Notion table the bot cannot reach (HTTP 404: not shared with the
  integration), a Gmail token that does not work, an Anthropic key Anthropic refuses, the
  /fetch gate closed (/update is due).
- ⚠️ Needs you soon: a token that stops within 2 days, the /fetch gate closing within 5
  days, a contact provider with no credit left, the monthly LinkedIn export, the autopilot
  schedule switched off.
- ✅ The rest: AI cost this month, credits, the last /fetch, today's run times.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from jobengine.screen import schedule
from jobengine.track import daily_report

SWITCH_KEY = "schedule.health"
TIME_KEY = "schedule.health_time"
SENT_KEY = "health.sent"  # bot_state: {"date": <local iso date of the last check>}
DEFAULT_TIME = "06:30"
WINDOW = timedelta(hours=3)
TOKEN_SOON_DAYS = 2
GATE_SOON_DAYS = 5
# Read tables the run needs (the write tables are all checked).
READ_TABLES = ("target_companies", "config", "strategy", "skills_inventory", "term_map")
TABLE_NAMES = {"job_opportunities": "Job Opportunities", "contacts": "Contacts",
               "resume_log": "Resume Log", "bot_state": "Bot State",
               "answer_bank": "Answer bank", "autoapply_issues": "Auto-apply issues",
               "target_companies": "Target Companies", "config": "Config",
               "strategy": "Strategy", "skills_inventory": "Skills Inventory",
               "term_map": "Term Map", "weekly_audit": "Weekly Audit"}


def is_on(config: Any) -> bool:
    return (config.get(SWITCH_KEY) or "true").strip().lower() != "false"


def due(config: Any, state: Any, now: datetime) -> bool:
    """True when today's check has not gone out and its time came within the window."""
    if not is_on(config):
        return False
    here = daily_report.local(now, config)
    at = schedule._time(config.get(TIME_KEY), DEFAULT_TIME)
    start = datetime.combine(here.date(), at, tzinfo=here.tzinfo)
    if not start <= here <= start + WINDOW:
        return False
    sent = (state.get(SENT_KEY) if state is not None else None) or {}
    return sent.get("date") != here.date().isoformat()


def mark_sent(state: Any, day: date) -> None:
    if state is not None:
        state.set(SENT_KEY, {"date": day.isoformat()})


def tables(read: Mapping[str, str], write: Mapping[str, str]) -> dict[str, str]:
    """name -> data source id of the tables the run needs."""
    wanted = {k: read[k] for k in READ_TABLES if read.get(k)}
    wanted.update({k: v for k, v in write.items() if v})
    return {TABLE_NAMES.get(k, k): v for k, v in wanted.items()}


def table_problems(found: Mapping[str, str],
                   query: Callable[[str], Any]) -> list[str]:
    """One line per table the bot cannot read (`query` reads one row)."""
    out = []
    for name, source in found.items():
        try:
            query(source)
        except Exception as exc:  # report every table, never stop at the first
            status = getattr(exc, "status", None)
            if status == 404:
                out.append(f"Notion table {name}: not shared with the bot (HTTP 404). Fix: "
                           "open it in Notion, ••• > Connections, add the Job Engine "
                           "integration.")
            else:
                out.append(f"Notion table {name}: {' '.join(str(exc).split())[:120]}")
    return out


def gate_days(config: Any, today: date) -> int | None:
    """Days the /fetch gate stays open (negative: closed); None when not set."""
    updated = config.get_date_prefix("last_strategy_update")
    days = config.get_int("strategy_refresh_days")
    if updated is None or days is None:
        return None
    return days - (today - updated).days


def text(day: date, blockers: Iterable[str], soon: Iterable[str], fine: Iterable[str],
         starts: str = "") -> str:
    blockers, soon, fine = list(blockers), list(soon), list(fine)
    when = f" (before the {starts} run)" if starts else ""
    lines = [f"🩺 Morning check, {day:%a %d %b}{when}"]
    if blockers:
        lines.append("⛔ Blocks the run, fix first:")
        lines += [f"- {line}" for line in blockers]
    if soon:
        lines.append("⚠️ Needs you soon:")
        lines += [f"- {line}" for line in soon]
    if not blockers and not soon:
        lines.append("✅ Nothing blocks today's run.")
    lines += [f"✅ {line}" for line in fine]
    return "\n".join(lines)
