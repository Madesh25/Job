"""Clean inbox (C2, your request of 23 Oct: alert mails made the inbox look like spam).

After /fetch has read the job alerts and saved their jobs, each alert mail it found at least
one job in is archived (out of the Inbox) and marked read, in its own mailbox (m02 or mm). It
keeps the "Job Alerts" label, so the next /fetch and Gmail search still find it. When a label
"Job Alerts/<Board>" exists in that mailbox (for example "Job Alerts/LinkedIn"; make it in
Gmail, the bot never creates labels) the mail gets it too. A mail in Spam is moved out of it.

Never touched: a mail the bot found no job in (a LinkedIn message from a recruiter, a
"your profile was viewed" note, a newsletter), replies and anything else. Nothing is deleted,
trashed or sent.

On only when Notion Config `alerts.tidy` is "true" (off by default). It only archives and
marks read your own alert mails, so it does not wait for DRY_RUN to be off. The alert tokens
need the gmail.modify scope (a token renewed with /gmailtoken has it); a token without it is
named in the /fetch summary and its mails stay as they are.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger("jobengine.sweep")

CONFIG_KEY = "alerts.tidy"
INBOX, UNREAD, SPAM = "INBOX", "UNREAD", "SPAM"
# One alert mail: (message id, Board, in Spam).
Found = Mapping[str, list[tuple[str, str, bool]]]


def enabled(config: Any) -> bool:
    return str(config.get(CONFIG_KEY) or "").strip().lower() == "true"


def _client(token_json: str) -> Any:
    from jobengine import gmail_reader
    from jobengine.gmail_client import GmailClient

    return GmailClient(gmail_reader.build_service(token_json))


def _short(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    if "insufficient" in text.lower() or "403" in text:
        return "its token cannot change mail (renew it with /gmailtoken to add gmail.modify)"
    return f"{type(exc).__name__}: {text[:150]}"


def tidy(tokens: Mapping[str, str], found: Found, label: str,
         client_for: Callable[[str], Any] = _client) -> list[str]:
    """Archive and mark read the alert mails in `found` (mailbox -> mails); returns the lines
    for the /fetch summary. One mailbox failing never stops the other."""
    lines: list[str] = []
    done: dict[str, int] = {}
    for mailbox, mails in found.items():
        token = tokens.get(mailbox)
        if not mails or not token:
            continue
        try:
            client = client_for(token)
            labels = client.labels()
        except Exception as exc:  # the Google client raises many error types
            lines.append(f"Inbox tidy skipped for {mailbox}: {_short(exc)}")
            continue
        count = 0
        for message_id, board, in_spam in mails:
            add = [labels[name] for name in (label, f"{label}/{board}") if name in labels]
            remove = [INBOX, UNREAD, *([SPAM] if in_spam else [])]
            try:
                client.modify_labels(message_id, add, remove)
                count += 1
            except Exception as exc:  # stop this mailbox at the first refusal
                lines.append(f"Inbox tidy stopped for {mailbox} after {count}: {_short(exc)}")
                break
        done[mailbox] = count
    if any(done.values()):
        parts = ", ".join(f"{name} {n}" for name, n in done.items() if n)
        lines.insert(0, f"Inbox tidy: {sum(done.values())} alert mail(s) archived and marked "
                        f"read ({parts}); they keep the {label} label.")
    return lines
