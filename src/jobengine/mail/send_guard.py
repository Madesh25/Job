"""Safe sending (Phase 12, 28 Oct): who the bot may mail by itself, and a stop after bounces.

While a new sender warms up, a few bounces can hurt the Gmail account's reputation. In mail
mode "send" the bot therefore sends a mail by itself only when:

- the address is trustworthy: a provider verified it (Contacts Status "Verified"), or the
  company wrote it itself (Source "Job posting" or "Careers page"). An address a provider
  found but could not verify (Hunter "accept all", GitHub commits ...) stays a Gmail draft:
  you look at it and send it yourself. Notion Config `mail.send_unverified` = `true` lets the
  bot send those too;
- the company's domain receives mail: a free DNS look-up for its mail servers (MX records,
  Google's public DNS over https). A domain without mail servers keeps the draft. A look-up
  that fails does not stop the mail (the other checks still apply).

Bounce guard: when MAX_DAY bounces arrive in one day, or at least MIN_WEEK bounces and more
than WEEK_SHARE of the last 7 days' mails, sending pauses by itself and Telegram says why.
Drafts are still written. `/mailmode send` starts sending again after you checked the
bounced addresses' source.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Any

log = logging.getLogger("jobengine.mail")

TRUSTED_SOURCES = ("Job posting", "Careers page")
SEND_UNVERIFIED_KEY = "mail.send_unverified"
GUARD_KEY = "mail.bounce_guard"  # bot_state: {"days": {iso: [sent, bounced]}, "paused": ...}
MAX_DAY = 2
MIN_WEEK = 2
WEEK_SHARE = 0.05
KEEP_DAYS = 14
DNS_URL = "https://dns.google/resolve"

MxLookup = Callable[[str], bool | None]  # domain -> receives mail (None: unknown)


# ---------------------------------------------------------------- who may be mailed


def send_unverified(config: Any) -> bool:
    return (config.get(SEND_UNVERIFIED_KEY) or "false").strip().lower() == "true"


def address_problem(contact: Mapping[str, Any] | None, email: str, config: Any,
                    mx: MxLookup | None) -> str | None:
    """Why the bot must not send this mail by itself (it stays a draft), or None."""
    contact = contact or {}
    # Drafting moves the Status to Drafted and notes "(was Unverified)" (mail/drafter.py).
    unverified = contact.get("Status") == "Unverified" or "(was Unverified)" in str(
        contact.get("Notes") or "")
    source = str(contact.get("Source") or "")
    if unverified and source not in TRUSTED_SOURCES and not send_unverified(config):
        return (f"the address is not verified ({source or 'unknown source'}): check it and "
                "send it from Gmail")
    domain = email.rpartition("@")[2].strip().lower()
    if mx is not None and domain and mx(domain) is False:
        return f"{domain} receives no mail (no mail server in its DNS)"
    return None


def mx_lookup(get_json: Callable[[str, Mapping[str, Any]], Any]) -> MxLookup:
    """A cached MX look-up through DNS over https. None when the look-up itself failed."""
    seen: dict[str, bool | None] = {}

    def lookup(domain: str) -> bool | None:
        if domain in seen:
            return seen[domain]
        try:
            answer = get_json(DNS_URL, {"name": domain, "type": "MX"}) or {}
            status = answer.get("Status")
            found = any(a.get("type") == 15 for a in answer.get("Answer") or [])
            # Status 3 (NXDOMAIN): the domain does not exist; 0 with no MX: no mail server.
            result: bool | None = found if status in (0, 3) else None
        except Exception as exc:  # never stop a mail over the look-up itself
            log.info("MX look-up failed for %s: %s", domain, exc)
            result = None
        seen[domain] = result
        return result

    return lookup


# ---------------------------------------------------------------- bounce guard


def _load(state: Any) -> dict[str, Any]:
    return dict((state.get(GUARD_KEY) if state is not None else None) or {})


def _save(state: Any, value: dict[str, Any], today: date) -> None:
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    value["days"] = {d: v for d, v in (value.get("days") or {}).items() if d >= cutoff}
    state.set(GUARD_KEY, value)


def paused(state: Any) -> str | None:
    """Why sending is paused, or None."""
    return _load(state).get("paused") if state is not None else None


def record_sent(state: Any, today: date) -> None:
    if state is None:
        return
    value = _load(state)
    days = dict(value.get("days") or {})
    sent, bounced = (list(days.get(today.isoformat()) or [0, 0]) + [0, 0])[:2]
    days[today.isoformat()] = [sent + 1, bounced]
    value["days"] = days
    _save(state, value, today)


def record_bounces(state: Any, today: date, count: int) -> str | None:
    """Count today's bounces; pause sending when there are too many. The alert text when the
    pause starts now, else None."""
    if state is None or count <= 0:
        return None
    value = _load(state)
    days = dict(value.get("days") or {})
    sent, bounced = (list(days.get(today.isoformat()) or [0, 0]) + [0, 0])[:2]
    days[today.isoformat()] = [sent, bounced + count]
    value["days"] = days
    week = [v for d, v in days.items() if d > (today - timedelta(days=7)).isoformat()]
    week_sent = sum(int(v[0]) for v in week)
    week_bounced = sum(int(v[1]) for v in week)
    why = None
    if bounced + count >= MAX_DAY:
        why = f"{bounced + count} mails bounced today"
    elif week_bounced >= MIN_WEEK and week_sent and week_bounced / week_sent > WEEK_SHARE:
        why = (f"{week_bounced} of {week_sent} mails bounced in the last 7 days "
               f"({week_bounced / week_sent:.0%})")
    alert = None
    if why and not value.get("paused"):
        value["paused"] = why
        alert = (f"🛑 Sending paused: {why}. Bounces hurt your Gmail account in the warm-up "
                 "weeks. Drafts are still written. Look at the bounced addresses in Contacts "
                 "(Status Bounced, their Source), then send /mailmode send to start sending "
                 "again.")
    _save(state, value, today)
    return alert


def resume(state: Any, today: date) -> bool:
    """/mailmode send: clear a pause. True when one was cleared."""
    if state is None:
        return False
    value = _load(state)
    if not value.get("paused"):
        return False
    value.pop("paused", None)
    _save(state, value, today)
    return True
