"""Location and work email of your LinkedIn connections (flow feature 1, step 3, 23 Oct).

The LinkedIn export has no location, and the bot never opens LinkedIn. Two paid providers
fill the 🤝 LinkedIn Connections table instead:

- Prospeo /enrich-person by the person's LinkedIn profile link: their country (Location
  source Prospeo) and a VERIFIED work email; one credit when it finds the person.
- Hunter /email-finder by name and the company's email domain (Target Companies Domain): a
  work email; one credit when it finds one. Hunter gives no location.

When it runs:

1. On demand: "Location and email" under a job's connections looks up at most MAX_ON_DEMAND
   people at that company (Prospeo).
2. Your idea of 23 Oct, the leftovers: credits not used by the renewal day are lost, so on
   the last day before a provider renews (Prospeo says how many days are left; Hunter gives
   its reset date) the bot spends what is left, at most MAX_LEFTOVER people, on your
   connections at Target Companies first, then the rest. Engineers, managers and HR only;
   never juniors, never a Removed person, never someone already filled.

Paid calls follow the usual rule (safety.paid_api_allowed: prod with DRY_RUN off, or your
PC with CONTACTS_LIVE=true). Only empty fields are written; a personal address is never
stored as a work email.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from jobengine import http
from jobengine.contacts import connections as conn
from jobengine.contacts.classify import is_junior, is_personal, valid_email

log = logging.getLogger("jobengine.contacts")

PROSPEO_ENRICH = "https://api.prospeo.io/enrich-person"
HUNTER_FINDER = "https://api.hunter.io/v2/email-finder"
MAX_ON_DEMAND = 3
MAX_LEFTOVER = 60  # people per provider per renewal (about 2 minutes of calls)
PROSPEO_PAUSE = 1.5  # seconds between Prospeo calls (free plan: one person a call)
DONE_KEY = "connections.leftover"  # bot_state: {provider: <renewal date the leftovers used>}
CHECKED_KEY = "connections.leftover_checked"  # bot_state: {"date": <iso>}, once a day


@dataclass
class Outcome:
    filled: int = 0
    credits: int = 0
    asked: int = 0
    no_match: int = 0  # people the provider has no match for (no credit, skipped)
    notes: list[str] = field(default_factory=list)


# A provider's "nobody found" answers: one person skipped, never the run stopped.
NO_MATCH = re.compile(r"NO_MATCH|NO_RESULTS|HTTP 404\b")


Request = Callable[..., Any]


def _work_email(email: str) -> bool:
    return valid_email(email) and not is_personal(email.lower())


def prospeo_props(row: conn.StoredRow, answer: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """(props to write, found) from an /enrich-person answer."""
    if answer.get("error"):
        return {}, False  # NO_MATCH and the like: no credit
    person = answer.get("person") or {}
    props: dict[str, Any] = {}
    country = (person.get("location") or {}).get("country")
    if country and not row.info.get("Country"):
        props["Country"] = ("rich_text", str(country))
        props["Location source"] = ("select", "Prospeo")
    mail = person.get("email") or {}
    email = str(mail.get("email") or "") if isinstance(mail, dict) else str(mail)
    status = mail.get("status") if isinstance(mail, dict) else None
    if status == "VERIFIED" and _work_email(email) and not row.info.get("Work email"):
        props["Work email"] = ("email", email)
    return props, True


def prospeo_lookup(row: conn.StoredRow, request: Request, key: str) -> tuple[dict, bool]:
    body = {"data": {"linkedin_url": row.person.url}, "only_verified_email": True,
            "enrich_mobile": False}
    answer = request("POST", PROSPEO_ENRICH, json_body=body,
                     headers={"X-KEY": key, "Content-Type": "application/json"}) or {}
    return prospeo_props(row, answer)


def hunter_lookup(row: conn.StoredRow, domain: str, request: Request,
                  key: str) -> tuple[dict, bool]:
    found = request("GET", HUNTER_FINDER, params={
        "domain": domain, "first_name": row.person.first, "last_name": row.person.last,
        "api_key": key}) or {}
    data = found.get("data") or {}
    email = str(data.get("email") or "")
    if not email:
        return {}, False
    status = (data.get("verification") or {}).get("status")
    if status == "invalid" or not _work_email(email) or row.info.get("Work email"):
        return {}, True  # a credit was spent, nothing worth keeping
    return {"Work email": ("email", email)}, True


def _wanted(row: conn.StoredRow) -> bool:
    return (not row.removed and not is_junior(row.person.position)
            and conn.kind(row.person.position) != conn.OTHER_KIND)


def _target_of(row: conn.StoredRow, targets: Iterable[Any]) -> Any:
    return next((t for t in targets if conn.same_company(row.person.company, t.name)), None)


def prospeo_picks(rows: Iterable[conn.StoredRow], targets: list[Any],
                  limit: int) -> list[conn.StoredRow]:
    """People without a country and with a profile link; Target Companies first."""
    found = [r for r in rows if _wanted(r) and r.person.url and not r.info.get("Country")]
    found.sort(key=lambda r: (_target_of(r, targets) is None, r.person.name))
    return found[:limit]


def hunter_picks(rows: Iterable[conn.StoredRow], targets: list[Any],
                 limit: int) -> list[tuple[conn.StoredRow, str]]:
    """People without a work email at a Target Company whose email domain is known."""
    out = []
    for row in rows:
        if not _wanted(row) or row.info.get("Work email"):
            continue
        target = _target_of(row, targets)
        if target is not None and getattr(target, "domain", None):
            out.append((row, str(target.domain)))
    out.sort(key=lambda pair: pair[0].person.name)
    return out[:limit]


def run(store: conn.ConnectionsStore, picks: list[Any], lookup: Callable[[Any], tuple],
        pause: Callable[[], None] = lambda: None) -> Outcome:
    """Look each pick up, write what was found. A refusal stops the run (HTTP 429 after the
    waits, or no credits left); "no match" for one person only skips that person (Phase 12
    P12-03, 28 Oct: one NO_MATCH stopped the whole batch after 0 people)."""
    out = Outcome()
    for i, pick in enumerate(picks):
        row = pick[0] if isinstance(pick, tuple) else pick
        if i:
            pause()
        try:
            props, spent = lookup(pick)
        except http.HttpError as exc:
            if NO_MATCH.search(str(exc)):
                out.asked += 1
                out.no_match += 1
                continue
            out.notes.append(f"stopped after {out.asked}: {exc}")
            break
        out.asked += 1
        out.credits += int(spent)
        if props:
            store.update(row.page_id, props)
            out.filled += 1
    return out


def leftover_due(state: Any, provider: str, left: int | None, renews: date | None,
                 today: date) -> bool:
    """True on the last day before the renewal, with credits left, once per renewal."""
    if state is None or not left or left <= 0 or renews is None:
        return False
    if (renews - today).days > 1:
        return False
    done = (state.get(DONE_KEY) or {}).get(provider)
    return done != renews.isoformat()


def mark_done(state: Any, provider: str, renews: date) -> None:
    done = dict(state.get(DONE_KEY) or {})
    done[provider] = renews.isoformat()
    state.set(DONE_KEY, done)


def checked_today(state: Any, today: date) -> bool:
    """The balances are asked once a day, not on every tick of the bot."""
    if (state.get(CHECKED_KEY) or {}).get("date") == today.isoformat():
        return True
    state.set(CHECKED_KEY, {"date": today.isoformat()})
    return False


def renewal_from_days(days: Any, today: date) -> date | None:
    try:
        return today + timedelta(days=int(days))
    except (TypeError, ValueError):
        return None


def renewal_from_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def renewal_from_day(day: Any, today: date) -> date | None:
    """The next renewal on this day of the month (today included)."""
    try:
        day = int(day)
    except (TypeError, ValueError):
        return None
    if not 1 <= day <= 28:
        return None
    this_month = today.replace(day=day)
    if this_month >= today:
        return this_month
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(year, month, day)


def summary(provider: str, out: Outcome, why: str) -> str:
    text = (f"{provider}: {out.asked} of your LinkedIn connections looked up ({why}), "
            f"{out.filled} filled in 🤝 LinkedIn Connections, {out.credits} credit(s) used.")
    if out.no_match:
        text += f" {out.no_match} not known to {provider} (skipped, no credit)."
    return text + "".join(f"\n- {note}" for note in out.notes)
