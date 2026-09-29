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
from datetime import UTC, date, datetime
from typing import Any

from jobengine.bot_state import BotState
from jobengine.drive_client import DriveError
from jobengine.gmail_client import GmailError
from jobengine.mail import preflight, timing, warmup
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
WINDOW_HINT = "/mailqueue shows the queue"


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
    queued: list[str] = field(default_factory=list)  # wait for the recipient's morning
    dry_run: bool = False
    used: int = 0
    cap: int = DEFAULT_DAILY_SENDS
    message: str = ""


def mode(state: BotState | None) -> str:
    value = (state.get(MODE_KEY) or {}) if state is not None else {}
    return SEND if value.get("mode") == SEND else DRAFT


def mode_command(state: BotState | None, args: str, deps: MailDeps | None = None) -> str:
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
        text = ("Mail mode: send. Drafts are written, checked (" + CHECKED + ") and sent "
                "when every check passes, within today's cap. Any problem keeps the mail as "
                "a draft. /mailmode draft to only write drafts.")
        if deps is not None:
            config = deps.config()
            text += "\n" + warmup.line(deps.s.mail, state, config, deps.today(),
                                       ceiling(deps, config))
        return text
    return ("Mail mode: draft. Mails are written as Gmail drafts and never sent; you send "
            "them from Gmail. /mailmode send to let the bot send the drafts that pass every "
            "check.")


def ceiling(deps: MailDeps, config: Any) -> int:
    """Config mail.daily_send_cap. Not set: 10, or the warm-up's last step when it is on."""
    if warmup.enabled(config) and config.get("mail.daily_send_cap") is None:
        default = warmup.steps(deps.s.mail)[-1]
    else:
        default = int(deps.s.mail.get("daily_send_cap", DEFAULT_DAILY_SENDS))
    return max(0, config.get_int("mail.daily_send_cap", default) or 0)


def daily_cap(deps: MailDeps, config: Any, state: BotState | None = None) -> int:
    """Today's cap: the warm-up step of this week (mail/warmup.py), never above the
    ceiling Config mail.daily_send_cap."""
    top = ceiling(deps, config)
    ramp = warmup.cap(deps.s.mail, state, config, deps.today())
    return top if ramp is None else min(top, ramp)


def sent_today(state: BotState | None, today: date) -> int:
    value = (state.get(SENT_KEY) or {}) if state is not None else {}
    return int(value.get("count") or 0) if value.get("date") == today.isoformat() else 0


def _record(state: BotState | None, today: date, count: int) -> None:
    if state is not None:
        state.set(SENT_KEY, {"date": today.isoformat(), "count": count})
        warmup.mark_first_send(state, today)


def _append(notes: str | None, line: str) -> str:
    notes = (notes or "").strip()
    return f"{notes}\n{line}" if notes else line


def send_checked(deps: MailDeps, state: BotState | None, drafts: DraftsResult) -> SendResult:
    """Check and send the drafts that `create_drafts` just wrote for one job."""
    config = deps.config()
    today = deps.today()
    s = deps.s
    dry = drafts.dry_run or not gmail_write_allowed(s)
    result = SendResult(dry_run=dry, cap=daily_cap(deps, config, state))
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
    window = timing.window(s.mail, config)
    now = deps.now()
    values = (deps.jobs.get_values(drafts.job_id) if deps.jobs is not None else None) or {}
    country = str(values.get("Country") or "")
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
            if window.enabled and not timing.inside(window, country, now):
                at = timing.next_start(window, country, now)
                kind = KIND.get(line.template, "mail")
                result.queued.append(f"{who} ({kind}): {timing.when_text(at)}")
                if gmail is not None:
                    timing.add(state, timing.QueueItem(
                        draft_id=line.draft_id, job_id=drafts.job_id, page_id=line.page_id,
                        name=who, to=expected.to, subject=mail.subject,
                        template=line.template, thread_id=line.thread_id, country=country,
                        signature=mail.signature.text.strip().splitlines()[0]
                        if mail.signature.text.strip() else "",
                        due=at.astimezone(UTC).isoformat()))
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
    if result.queued:
        verb = "Would wait" if result.dry_run else "Waiting"
        lines.append(f"{verb} for the recipient's morning ({WINDOW_HINT}), then sent by "
                     "itself after the same checks:")
        lines += [f"- {q}" for q in result.queued]
    if result.kept:
        lines.append("Kept as drafts, not sent:")
        lines += [f"- {k}" for k in result.kept]
    if drafts.skipped:
        lines.append(f"Skipped: {'; '.join(drafts.skipped)}")
    if not result.dry_run:
        lines.append(f"{result.used} of {result.cap} sends used today.")
    return "\n".join(lines)


# ---------------------------------------------------------------- the morning queue


def send_due(deps: MailDeps, state: BotState | None) -> str | None:
    """Send one waiting mail whose recipient's morning has come, after the same checks.
    Called every few minutes; None when nothing was due (or today's sends are used)."""
    config = deps.config()
    s = deps.s
    window = timing.window(s.mail, config)
    now = deps.now()
    due = timing.due_items(state, now, window)
    if not due or not gmail_write_allowed(s):
        return None
    today = deps.today()
    cap = daily_cap(deps, config, state)
    used = sent_today(state, today)
    if used >= cap:
        return None  # the rest wait for the next morning
    item = due[0]
    kind = KIND.get(item.template, "mail")
    gmail = deps.gmail()
    try:
        raw = gmail.draft_raw(item.draft_id)
    except Exception:  # sent or deleted in Gmail by you
        timing.remove(state, item.draft_id)
        return (f"{item.name}: the draft is no longer in Gmail (sent or deleted), so it left "
                "the mail queue.")
    try:
        attachment = None
        if (config.get("mail.attach_resume") or "yes").casefold() != "no":
            row = approved_resume(deps, item.job_id)
            if row is None:
                raise DriveError("no approved resume for this job")
            attachment = (resume_name(row), resume_pdf(deps, row))
    except (DriveError, GmailError) as exc:
        timing.remove(state, item.draft_id)
        return f"Not sent, kept as a draft: {item.name} ({exc})."
    expected = preflight.Expected(
        to=item.to, subject=item.subject, signature_text=item.signature,
        attachment=attachment, max_kb=_max_kb(deps, config),
        env_label=s.env_label if s.app_env != PROD else "")
    problems = preflight.check_raw(raw, expected)
    if problems:
        timing.remove(state, item.draft_id)
        return f"Not sent, kept as a draft: {item.name}: {'; '.join(problems)}."
    try:
        sent = gmail.send_draft(item.draft_id)
    except Exception as exc:  # Gmail refused it: it stays a draft
        log.warning("sending to %s failed: %s", item.to, exc)
        timing.remove(state, item.draft_id)
        return f"Not sent, kept as a draft: {item.name}: Gmail refused it ({exc})."
    timing.remove(state, item.draft_id)
    used += 1
    _record(state, today, used)
    notes = ""
    if deps.contacts is not None:
        notes = dict(deps.contacts.all_rows()).get(item.page_id, {}).get("Notes") or ""
    _mark_contacted(deps, item.page_id, notes, sent.thread_id or item.thread_id, today)
    if deps.jobs is not None:
        deps.jobs.update(item.job_id, {"Last activity date": today})
    local = timing.aware(now).astimezone(timing.zone(item.country))
    left = len(timing.load(state))
    return (f"Sent at {local:%H:%M} {local.tzname()} (the recipient's morning), after the "
            f"checks: {item.name} ({kind}) | To {item.to} | Subject: {item.subject}\n"
            f"{used} of {cap} sends used today. {left} "
            f"mail{'' if left == 1 else 's'} wait in the queue.")


def queue_text(deps: MailDeps, state: BotState | None) -> str:
    """/mailqueue: the mails waiting for their recipient's morning."""
    window = timing.window(deps.s.mail, deps.config())
    items = timing.load(state)
    if not window.enabled:
        head = "Send window: off (mails that pass the checks are sent at once)."
    else:
        head = f"Send window: {window.text()}."
    if not items:
        return f"{head}\nNo mail waits in the queue."
    lines = [head, f"{len(items)} mail{'' if len(items) == 1 else 's'} waiting:"]
    for item in sorted(items, key=lambda i: i.due):
        at = datetime.fromisoformat(item.due).astimezone(timing.zone(item.country))
        lines.append(f"- {item.name} ({KIND.get(item.template, 'mail')}), "
                     f"{timing.when_text(at)}: {item.subject}")
    lines.append("Each is checked again and sent by itself; one every few minutes.")
    return "\n".join(lines)
