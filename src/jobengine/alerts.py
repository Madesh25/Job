"""Mails to you, the owner (Autopilot H1 and H3, 25 Oct): the daily report and an alert when a
scheduled run fails. Telegram can be missed, so these also come by email.

They go to safety.redirect_to only (your own address), from the sender mailbox, and DRY_RUN
does not stop them: a report or an alert never reaches anyone else (your decision of 25 Oct,
safety.owner_mail_target). The same failure is mailed at most once an hour.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

from jobengine.settings import Settings

log = logging.getLogger("jobengine.alerts")

SENT_KEY = "alerts.mailed"  # bot_state: {"<what>": "<ISO time of the last alert mail>"}
REPEAT = timedelta(hours=1)
SEND_TO_CLAUDE = ("Send Claude Code this error line and the last 40 lines of the bot log "
                  "(PowerShell: Get-Content out\\bot-log.txt -Tail 40; Cloud Run: the "
                  "service's Logs tab). Never the .env or a token.")

Mailer = Callable[[str, str], str | None]  # (subject, text) -> None when sent, else why not


def owner_mail(s: Settings, subject: str, text: str) -> str | None:
    """Send a plain-text mail to you. None when sent, else the reason (also logged)."""
    from jobengine.gmail_client import GmailClient
    from jobengine.safety import owner_mail_target

    to = owner_mail_target(s)
    if not to:
        return "no owner address (safety.redirect_to)"
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = f"{s.env_label} {subject}".strip() if s.env_label else subject
    message.set_content(text)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        gmail = GmailClient.from_settings(s)
        gmail.send_draft(gmail.create_draft(raw).draft_id)
    except Exception as exc:  # a report must never stop the bot
        log.warning("could not mail you %r: %s", subject, exc)
        return str(exc) or type(exc).__name__
    return None


def failure_text(what: str, error: str, at: datetime) -> str:
    return (f"{what} failed at {at:%d %b %H:%M}.\n\nError: {error}\n\n{SEND_TO_CLAUDE}\n\n"
            "The bot keeps running; the next scheduled run tries again.")


def mail_failure(state: Any, mailer: Mailer, what: str, error: str, now: datetime) -> bool:
    """Mail you that `what` failed, unless the same alert went out within the last hour."""
    sent = dict((state.get(SENT_KEY) if state is not None else None) or {})
    last = sent.get(what)
    try:
        if last and now - datetime.fromisoformat(last) < REPEAT:
            return False
    except (TypeError, ValueError):
        pass
    mailer(f"Job Engine: {what} failed", failure_text(what, error, now))
    if state is not None:
        sent[what] = now.isoformat(timespec="seconds")
        try:
            state.set(SENT_KEY, sent)
        except Exception:
            log.exception("could not store %s", SENT_KEY)
    return True
