"""One morning message (S12, 23 Nov, your ask): the morning check and today's steps together.

The 06:30 morning check (track/health_report.py) ends with "📋 Today": what the bot runs
today (the job search and screening runs, Apply today, the daily report, the apply nudge and
the weekend checks) and your steps (the resumes built that wait for you to apply, the jobs in
/pending with how many are Apply high, the jobs of the last 3 days that wait for /jd). The
07:30 daily check then comes to Telegram only when it found something or something needs you;
a quiet one is kept in its Run Log row. Notion Config `morning.one_message` = false brings
back the morning check without the steps and the 07:30 check every day. Pure, but for the
numbers the desk passes in.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, time
from typing import Any

SWITCH_KEY = "morning.one_message"
NOON = time(12, 0)

# The weekend's checks (screen/timetable.py, "Every week").
WEEKLY = {5: ["11:00 new companies from the visa sponsor lists (/newcompanies)"],
          6: ["09:00 link check", "10:00 job board check", "19:30 screening check",
              "20:00 weekly digest and audit"]}


def is_on(config: Any) -> bool:
    try:
        return (config.get(SWITCH_KEY) or "true").strip().lower() != "false"
    except Exception:  # a Config read that fails keeps the one message
        return True


def bot_line(day: date, runs: Sequence[str], apply_today: str | None, report: str,
             nudge: str | None) -> str:
    """What the bot does today, in time order."""
    parts = []
    if runs:
        parts.append(f"{' and '.join(runs)} job search and screening")
    if apply_today:
        parts.append(f"{apply_today} Apply today")
    parts.append(f"{report} daily report")
    if nudge:
        parts.append(f"{nudge} apply nudge")
    parts += WEEKLY.get(day.weekday(), [])
    return "Bot: " + " · ".join(sorted(parts, key=lambda p: p[:5]))


def you_lines(ready: int | None, pending: int | None, high: int | None,
              jd: int | None) -> list[str]:
    """Your steps today; a number the bot could not read is left out."""
    lines = []
    if ready:
        lines.append(f"- Apply: {ready} job(s) with a built resume wait for you (apply on the "
                     "job page, then tap I applied)")
    if pending:
        extra = f" ({high} Apply high)" if high else ""
        lines.append(f"- Review: {pending} job(s) in /pending{extra}")
    if jd:
        lines.append(f"- /jd: {jd} job(s) of the last 3 days need a description")
    return lines or ["- Nothing waits for you yet: the morning run brings new jobs."]


def block(day: date, bot: str, you: Sequence[str]) -> str:
    return "\n\n" + "\n".join(["📋 Today", bot, "You:", *you])


def morning(at: time | None) -> bool:
    """The daily check run of the morning (07:30), not the evening one (21:30)."""
    return at is not None and at < NOON


def quiet(report: Any) -> bool:
    """A daily check with nothing for you: nothing found, nothing to decide, no problem.
    A DRY RUN note alone (follow-ups not drafted) counts as quiet."""
    return not (report.token_lines() or report.sent or report.deleted_drafts or report.replies
                or report.applications or report.bounced or report.followups
                or report.followups_queued or report.followup_problems
                or report.ghosted_contacts or report.ghosted_jobs or report.cards
                or report.notes or report.errors or report.retention is not None
                or report.applied_jobs)
