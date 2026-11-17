"""Bounce parsing (spec section 2 step 6). Pure."""

from __future__ import annotations

import re

from jobengine.track.models import Message

DAEMONS = ("mailer-daemon", "postmaster")
ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DSN_LINE = re.compile(r"^\s*(?:Final|Original)-Recipient:\s*(?:rfc822\s*;)?\s*<?([^>\s]+)>?",
                      re.IGNORECASE | re.MULTILINE)


def is_bounce(message: Message) -> bool:
    return message.sender.partition("@")[0] in DAEMONS


def failed_recipients(message: Message) -> list[str]:
    """X-Failed-Recipients, else the Final-Recipient / Original-Recipient lines of the
    delivery status part. Lowercase, in order, without duplicates."""
    found = ADDRESS.findall(message.headers.get("x-failed-recipients", ""))
    if not found:
        found = [m for m in DSN_LINE.findall(message.text) if ADDRESS.fullmatch(m)]
    return list(dict.fromkeys(a.lower() for a in found))
