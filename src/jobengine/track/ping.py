"""Instant reply ping (PR 14): a Telegram message within minutes of a reply.

Every `tracking.reply_check_minutes` (15) the inbox is searched once for new mail since the
last check. A message is pinged when it is:

- in the thread of a mail you sent to a contact (Contacted or Followed up), or
- in a job's application thread, or from the company's own domain of a job you applied to.

Nothing is written to Notion and no AI is used: the daily check (or /today) still reads,
classifies and records the reply as before. Each message is pinged once (Bot State
`tracking.pinged` keeps the last message IDs and the time of the last check). Automatic
replies, bounces and your own mails are never pinged.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from jobengine.track import bounces, reply_draft
from jobengine.track.classify import is_auto_reply
from jobengine.track.models import Message
from jobengine.track.runner import Run, TrackDeps, company_domain, strip_preview
from jobengine.track.status import JOB_TERMINAL

log = logging.getLogger("jobengine.track")

PINGED_KEY = "tracking.pinged"  # bot_state: {"ids": [...], "at": <iso time of last check>}
MAX_IDS = 300
DEFAULT_MINUTES = 15
FIRST_LOOK = timedelta(hours=24)
OVERLAP = timedelta(minutes=10)  # Gmail's index can lag a little behind
REPLY_CONTACTS = ("Contacted", "Followed up", "Replied")
APPLIED_JOBS = ("Applied", "Screening", "Interview")
INTERVIEW_RE = re.compile(
    r"\b(interview|call|chat|meet|schedule|availability|available|slot|calendly|teams|zoom|"
    r"rozmow\w*|gesprek|sollicitatiegesprek)\b", re.I)


def minutes(deps: TrackDeps) -> float:
    return max(1.0, float(deps.s.tracking.get("reply_check_minutes", DEFAULT_MINUTES)))


def due(deps: TrackDeps, now: datetime) -> bool:
    last = (deps.state.get(PINGED_KEY) or {}).get("at")
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(minutes=minutes(deps))
    except ValueError:
        return True


INTERVIEW_HINT = "It may be about an interview or a call: answer soon."


def _ping(message: Message, who: str, what: str, draft: str | None = None) -> str:
    preview = strip_preview(message.text or message.snippet)
    hint = ""
    if INTERVIEW_RE.search(f"{message.subject} {message.text or message.snippet}"):
        hint = "\n" + INTERVIEW_HINT
    answer = f"\n\n{reply_draft.block(draft)}\n\n" if draft else "\n"
    return (f"\U0001F4E9 New reply from {who} ({what})\nFrom: {message.sender}\n"
            f"Subject: {message.subject}\n{preview}{hint}{answer}"
            "The daily check records it; send /today to record it now.")


def _draft(run: Run, message: Message, first: str | None,
           job: dict[str, Any] | None) -> str | None:
    """Feature 3: the answer to copy; a failure only loses the draft, never the ping."""
    try:
        return reply_draft.draft(run.s, run.config, message.text or message.snippet,
                                 today=run.today, first=first, job=job)
    except Exception as exc:
        log.warning("reply draft not written: %s", exc)
        return None


def check(deps: TrackDeps, now: datetime) -> list[str]:
    """New replies since the last check, one text each. Updates Bot State."""
    state = deps.state.get(PINGED_KEY) or {}
    seen: list[str] = list(state.get("ids") or [])
    since = now - FIRST_LOOK
    if state.get("at"):
        try:
            since = datetime.fromisoformat(state["at"]) - OVERLAP
        except ValueError:
            pass
    run = Run(deps, now)
    contact_threads: dict[str, dict[str, Any]] = {
        c["Gmail thread ID"]: c for c in run.contacts.values()
        if c.get("Gmail thread ID") and c.get("Status") in REPLY_CONTACTS}
    job_threads: dict[str, dict[str, Any]] = {}
    job_domains: dict[str, dict[str, Any]] = {}
    for job in run.jobs.values():
        if job.get("Status") in JOB_TERMINAL:
            continue
        if job.get("Gmail thread ID"):
            job_threads[job["Gmail thread ID"]] = job
        if job.get("Status") in APPLIED_JOBS:
            domain = company_domain(run, job.get("Company") or "")
            if domain:
                job_domains[domain.casefold()] = job
    texts: list[str] = []
    query = f"after:{int(since.timestamp())} -in:chats -in:sent"
    for message in run.gmail.search(query, limit=50):
        if message.id in seen or message.sent or message.sender in run.mine:
            continue
        if is_auto_reply(message) or bounces.is_bounce(message):
            continue
        contact = contact_threads.get(message.thread_id)
        job = job_threads.get(message.thread_id) or job_domains.get(message.sender_domain)
        if contact is not None:
            who = contact.get("Name") or message.sender
            related = run.related_jobs(contact)
            first = (contact.get("Name") or "").split()[0] if contact.get("Name") else None
            draft = _draft(run, message, first, run.jobs[related[0]] if related else None)
            texts.append(_ping(message, who, run.company_of(contact) or "a contact", draft))
        elif job is not None:
            texts.append(_ping(message, job.get("Company") or message.sender,
                               f"your application for {job.get('Role') or 'the job'}",
                               _draft(run, message, None, job)))
        else:
            continue
        seen.append(message.id)
    deps.state.set(PINGED_KEY, {"ids": seen[-MAX_IDS:], "at": now.isoformat()})
    return texts
