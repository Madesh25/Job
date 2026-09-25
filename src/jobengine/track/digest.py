"""Weekly digest (spec section 6.3): this week against the week before, plus what waits for
you. Triggered by Module 09 on Sunday 20:00 Asia/Kolkata, and by /digest."""

from __future__ import annotations

from datetime import datetime, timedelta

from jobengine.screen.runner import pending_rows, waiting_for_jd
from jobengine.track import commands
from jobengine.track.runner import TrackDeps
from jobengine.track.stats import Funnel, compute, pct

FIELDS = ("applied", "replied", "screening", "interview", "offer", "rejected", "ghosted")


def _compare(now: Funnel, before: Funnel) -> str:
    parts = []
    for name in FIELDS:
        a, b = getattr(now, name), getattr(before, name)
        parts.append(f"{name} {a} ({'+' if a >= b else ''}{a - b})")
    return ", ".join(parts)


def run_weekly_digest(deps: TrackDeps, now: datetime) -> str:
    today = now.date()
    jobs = [v for _, v in deps.jobs.query_rows()] if deps.jobs else []
    contacts = [v for _, v in deps.contacts.all_rows()] if deps.contacts else []
    this = compute(jobs, contacts, today, 7, "This week")
    before = compute(jobs, contacts, today - timedelta(days=7), 7, "Week before")
    config = deps.config()
    lines = [
        f"Weekly digest {today.isoformat()}",
        f"This week: {_compare(this.funnel, before.funnel)}",
        f"Reply rate: {pct(this.funnel.rate)} (week before {pct(before.funnel.rate)})",
    ]
    if deps.jobs is not None:
        lines.append(f"Waiting in /pending: {len(pending_rows(deps.jobs))}")
        lines.append(f"Waiting for a JD (/jd): {len(waiting_for_jd(deps.jobs))}")
    drafts, followup_drafts = commands.unsent_drafts(contacts)
    lines.append(f"Unsent drafts: {drafts} cold mails, {followup_drafts} follow-ups")
    due = commands.followups_due(contacts, today, commands.followup_days(deps, config), 0)
    lines.append(f"Follow-ups due: {len(due)}")
    lines.append(commands.strategy_line(config, today))
    return "\n".join(lines)
