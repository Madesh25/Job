"""Mail mode (Telegram /mailmode): "draft" keeps every mail as a Gmail draft (the default);
"send" sends the drafts that pass every check, then shows what was sent.

In "send" mode the drafts are written first, exactly as in "draft" mode. Each one is then
checked twice (mail/preflight.py): the message the bot built, and the draft read back from
Gmail. Only a draft with no problem is sent, with Gmail's drafts.send, at most
`mail.daily_send_cap` (Config, default 10) a day. A draft with any problem stays in Gmail
Drafts and the reason is shown. With DRY_RUN on, the checks run and nothing is sent.
Outside prod every mail goes to your redirect address (safety.route_recipients).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from jobengine.bot_state import BotState
from jobengine.drive_client import DriveError
from jobengine.gmail_client import GmailError
from jobengine.mail import preflight
from jobengine.mail.drafter import (
    DraftsResult,
    MailDeps,
    _max_kb,
    approved_resume,
    env_where,
    resume_name,
    resume_pdf,
)
from jobengine.mail.templates import KIND
from jobengine.safety import PROD, gmail_write_allowed, route_recipients

log = logging.getLogger("jobengine.mail")

MODE_KEY = "mail.mode"  # bot_state: {"mode": "draft" | "send"}
SENT_KEY = "mail.sent_day"  # bot_state: {"date": <iso date>, "count": <mails sent>}
DRAFT, SEND = "draft", "send"
DEFAULT_DAILY_SENDS = 10
CHECKED = "To, no Cc or Bcc, subject, body, signature, resume attachment, Gmail's copy"


@dataclass
class SentLine:
    name: str
    to: str
    subject: str
    attachment: str
    kind: str = "mail"  # referral ask (engineers) or cold mail (HR, recruiters, hiring)


@dataclass
class SendResult:
    sent: list[SentLine] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    dry_run: bool = False
    used: int = 0
    cap: int = DEFAULT_DAILY_SENDS
    message: str = ""


def mode(state: BotState | None) -> str:
    value = (state.get(MODE_KEY) or {}) if state is not None else {}
    return SEND if value.get("mode") == SEND else DRAFT


def mode_command(state: BotState | None, args: str) -> str:
    """/mailmode, /mailmode draft, /mailmode send."""
    word = args.strip().lower()
    if word in (DRAFT, SEND):
        if state is None:
            return "The mail mode cannot be saved here (no Bot State)."
        state.set(MODE_KEY, {"mode": word})
    current = mode(state)
    if word and word not in (DRAFT, SEND):
        return f"Unknown mail mode {word!r}. Send /mailmode draft or /mailmode send."
    if current == SEND:
        return ("Mail mode: send. Drafts are written, checked (" + CHECKED + ") and sent "
                "when every check passes, at most Config mail.daily_send_cap a day (10 when "
                "not set). Any problem keeps the mail as a draft. /mailmode draft to only "
                "write drafts.")
    return ("Mail mode: draft. Mails are written as Gmail drafts and never sent; you send "
            "them from Gmail. /mailmode send to let the bot send the drafts that pass every "
            "check.")


def daily_cap(deps: MailDeps, config: Any) -> int:
    default = int(deps.s.mail.get("daily_send_cap", DEFAULT_DAILY_SENDS))
    return max(0, config.get_int("mail.daily_send_cap", default) or 0)


def sent_today(state: BotState | None, today: date) -> int:
    value = (state.get(SENT_KEY) or {}) if state is not None else {}
    return int(value.get("count") or 0) if value.get("date") == today.isoformat() else 0


def _record(state: BotState | None, today: date, count: int) -> None:
    if state is not None:
        state.set(SENT_KEY, {"date": today.isoformat(), "count": count})


def _append(notes: str | None, line: str) -> str:
    notes = (notes or "").strip()
    return f"{notes}\n{line}" if notes else line


def send_checked(deps: MailDeps, state: BotState | None, drafts: DraftsResult) -> SendResult:
    """Check and send the drafts that `create_drafts` just wrote for one job."""
    config = deps.config()
    today = deps.today()
    s = deps.s
    dry = drafts.dry_run or not gmail_write_allowed(s)
    result = SendResult(dry_run=dry, cap=daily_cap(deps, config))
    used = sent_today(state, today)
    try:
        attachment = None
        if (config.get("mail.attach_resume") or "yes").casefold() != "no":
            row = approved_resume(deps, drafts.job_id)
            if row is None:
                raise DriveError("no approved resume for this job")
            attachment = (resume_name(row), resume_pdf(deps, row))
        gmail = None if dry else deps.gmail()
    except (DriveError, GmailError) as exc:
        result.kept = [f"all ({exc})"]
        result.message = send_summary(deps, drafts, result)
        return result
    max_kb = _max_kb(deps, config)
    notes: dict[str, str] = {}  # Contacts Notes, read once when the first mail goes out
    label = s.env_label if s.app_env != PROD else ""
    for line, mail in zip(drafts.drafted, drafts.mails, strict=True):
        who = line.name or line.email
        expected = preflight.Expected(
            to=route_recipients([line.email], [], "", s)[0][0], subject=mail.subject,
            signature_text=mail.signature.text, attachment=attachment, max_kb=max_kb,
            env_label=label)
        problems = preflight.check(mail.message, expected)
        try:
            if not problems and gmail is not None:
                problems = preflight.check_raw(gmail.draft_raw(line.draft_id), expected)
            if problems:
                result.kept.append(f"{who}: {'; '.join(problems)}")
                continue
            if used >= result.cap:
                result.kept.append(f"{who}: today's {result.cap} sends are used")
                continue
            size = f" ({len(attachment[1]) / 1024:.0f} KB)" if attachment else ""
            sent_line = SentLine(who, mail.to, mail.subject,
                                 f"{attachment[0]}{size}" if attachment else "none",
                                 KIND.get(line.template, "mail"))
            if gmail is None:
                log.warning("DRY RUN: would send the draft to %s", mail.to)
                result.sent.append(sent_line)
                continue
            sent = gmail.send_draft(line.draft_id)
        except Exception as exc:  # Gmail refused this one: it stays a draft
            log.warning("sending to %s failed: %s", line.email, exc)
            result.kept.append(f"{who}: Gmail refused it ({exc})")
            continue
        used += 1
        _record(state, today, used)
        result.sent.append(sent_line)
        if deps.contacts is not None and not notes:
            notes = {pid: v.get("Notes") or "" for pid, v in deps.contacts.all_rows()}
        _mark_contacted(deps, line.page_id, notes.get(line.page_id), sent.thread_id or
                        line.thread_id, today)
    if result.sent and not dry and deps.jobs is not None:
        deps.jobs.update(drafts.job_id, {"Last activity date": today})
    result.used = used
    result.message = send_summary(deps, drafts, result)
    return result


def _mark_contacted(deps: MailDeps, page_id: str, notes: str | None, thread_id: str,
                    today: date) -> None:
    """What the daily check does for a sent draft (Module 07), at once."""
    if deps.contacts is None:
        return
    deps.contacts.update(page_id, {
        "Status": "Contacted", "Last contacted": today, "Gmail draft ID": "",
        "Gmail thread ID": thread_id,
        "Notes": _append(notes, f"sent by the bot {today.isoformat()} after the checks")})


def send_summary(deps: MailDeps, drafts: DraftsResult, result: SendResult) -> str:
    total = len(drafts.drafted)
    head = f"Mails for {drafts.company}, {drafts.role}"
    lines = [head]
    if result.dry_run:
        lines.append(f"DRY RUN: {len(result.sent)} of {total} would be sent (every check "
                     "passed); nothing sent and no draft created.")
    else:
        lines.append(f"Sent {len(result.sent)} of {total} from {env_where(deps.s)} after the "
                     f"checks ({CHECKED}):")
    for item in result.sent:
        lines.append(f"- {item.name} ({item.kind}): To {item.to} | Subject: {item.subject} | "
                     f"Attachment: {item.attachment}")
    if result.kept:
        lines.append("Kept as drafts, not sent:")
        lines += [f"- {k}" for k in result.kept]
    if drafts.skipped:
        lines.append(f"Skipped: {'; '.join(drafts.skipped)}")
    if not result.dry_run:
        lines.append(f"{result.used} of {result.cap} sends used today.")
    return "\n".join(lines)
