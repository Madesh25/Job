"""Weekly digest (spec section 6.3): this week against the week before, plus what waits for
you. Triggered by Module 09 on Sunday 20:00 Asia/Kolkata, and by /digest."""

from __future__ import annotations

from datetime import datetime, timedelta

from jobengine.contacts import agencies
from jobengine.resume import header
from jobengine.screen.runner import pending_rows, waiting_for_jd
from jobengine.track import commands, costs, day_ledger, reply_analysis, what_works
from jobengine.track.runner import TrackDeps
from jobengine.track.stats import Funnel, compute, pct, replies_report

FIELDS = ("applied", "replied", "screening", "interview", "offer", "rejected", "ghosted")


def _compare(now: Funnel, before: Funnel) -> str:
    parts = []
    for name in FIELDS:
        a, b = getattr(now, name), getattr(before, name)
        parts.append(f"{name} {a} ({'+' if a >= b else ''}{a - b})")
    return ", ".join(parts)


def run_weekly_digest(deps: TrackDeps, now: datetime, main_title: str | None = None) -> str:
    """`main_title`: the headline your resume gets when the job's role names none of your
    approved titles (the Golden Master's first title, resume/header.py)."""
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
        lines.append(f"Waiting for a JD (/jd): {len(waiting_for_jd(deps.jobs, deps.state))}")
    drafts, followup_drafts = commands.unsent_drafts(contacts)
    lines.append(f"Unsent drafts: {drafts} cold mails, {followup_drafts} follow-ups")
    due = commands.followups_due(contacts, today, commands.followup_days(deps, config), 0)
    lines.append(f"Follow-ups due: {len(due)}")
    lines.append(commands.strategy_line(config, today))
    lines.append(costs.month_line(deps.state, today))
    agency_line = agencies.digest_line(agencies.load(deps.s), deps.state, today)
    if agency_line:  # feature 7
        lines.append(agency_line)
    if today.day <= 7:  # the first digest of the month (features 8 and 14)
        lines.append("Monthly: /keywords for your LinkedIn profile keywords, /gaps for the "
                     "learning plan.")
    lines += ["", replies_report(jobs, contacts, today)]
    lines += ["", what_works.text(jobs)]  # S6 (23 Nov): where to spend your time
    titles = header.approved_titles(deps.s, config)
    counts = reply_analysis.week_counts(lambda day: day_ledger.day(deps.state, day), today)
    lines += ["", reply_analysis.text(  # S9 (23 Nov): what got replies
        counts, jobs, lambda role: header.headline_title(role, titles) or main_title)]
    return "\n".join(lines)


# The one-line conclusions of the digest's analyses, kept in the short message (S10).
TIP_STARTS = ("Put more effort where it works:", "Most approvals for what it finds:",
              "Many found, none approved:", "Most answers:", "No answer yet after")


def short(full: str) -> str:
    """S10 (23 Nov, your ask): the Telegram digest, the week in numbers (the first block)
    and the tip of each analysis; the analyses in full are in the Run Log page."""
    head, _, rest = full.partition("\n\n")
    tips = [line for line in rest.splitlines() if line.startswith(TIP_STARTS)]
    return "\n".join([head, "", "📈 What gets replies, what works and the replies by resume "
                      "headline and skill: in the full report.", *tips])
