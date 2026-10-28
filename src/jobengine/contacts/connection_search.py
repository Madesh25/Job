"""Where your LinkedIn connections work, from a Google search (28 Oct, your request).

The LinkedIn export has no location, so most connections are "location unknown" and the
strict location rule cannot use them (all 14 at ING on 28 Oct). The bot never opens LinkedIn.
Instead the AI (Config model.people: Gemini with Google Search, or Claude with web search)
searches for the person's public LinkedIn result, the same way the contact search reads
LinkedIn result titles (contacts/web_search.py):

- The answer counts only when the search really returned the person's own profile link, the
  one in your export (linkedin.com/in/<id>). A connection without a profile link is never
  searched.
- Country: the result's address when it is a country site (nl., pl. or ie.linkedin.com), else
  the place the result shows when it names Poland, the Netherlands or Ireland or one of their
  cities. Written to Country with Location source "AI search"; only an empty Country is
  filled.
- Current job: when the result's title names another company, the person is shown as "may
  have left" under a job's connections (kept in bot_state, your Notes column is not touched).
- The email is never looked for here and never guessed: work emails still come only from
  Prospeo and Hunter (connection_lookup.py), or you send the LinkedIn message.

When: the "Location and email" button (at most MAX_ON_DEMAND people at that company), and once
a day from DAILY_HOUR for the companies of your approved jobs (at most MAX_DAILY people). A
person is searched again after RECHECK_DAYS.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from jobengine.contacts import connections as conn
from jobengine.contacts.classify import is_junior
from jobengine.contacts.providers.github import country_of
from jobengine.contacts.web_search import linkedin_country

log = logging.getLogger("jobengine.contacts")

STAGE = "people"  # the contact search's model (Config model.people)
KEY = "connections.searched"  # bot_state: {"people": {person key: {...}}, "day": iso}
RECHECK_DAYS = 30
MAX_ON_DEMAND = 6
MAX_DAILY = 20
DAILY_HOUR = 13  # your local time: after the morning runs
JOB_STATUSES = ("Approved", "Resume built", "Applied")
SOURCE = "AI search"  # an existing Location source option
TITLE_TAIL = re.compile(r"\s*[|\u00b7]\s*linkedin\s*$", re.I)

SYSTEM = """You find one person's public LinkedIn profile in web search results.
Search the web, for example: "First Last" Company site:linkedin.com/in
Answer with JSON only:
{"url": "<the profile link exactly as the search returned it>",
 "title": "<the search result's title as shown, e.g. Anna Lis - Platform Engineer - Acme>",
 "location": "<the place the search result shows, or empty>"}
Only this exact person (same first and last name, a linkedin.com/in/ link). Use only what the
search results show. When you are not sure, answer {"url": ""}."""


@dataclass
class Seen:
    """What one search showed about a connection."""

    country: str = ""
    title: str = ""
    other_company: str = ""  # the title names another company: may have left


@dataclass
class Outcome:
    searched: int = 0
    placed: list[str] = field(default_factory=list)  # "Anna Lis (Netherlands)"
    left: list[str] = field(default_factory=list)  # "Anna Lis (now Acme)"


# ---------------------------------------------------------------- one person


def title_company(title: str, name: str) -> str:
    """"Anna Lis - Platform Engineer - Acme | LinkedIn" -> "Acme" (the last part), only when
    the title starts with the person's name."""
    parts = [p.strip() for p in re.split(r"\s+[-\u2013\u2014]\s+", TITLE_TAIL.sub("", title))]
    if len(parts) < 3 or not conn.same_person(parts[0], name):
        return ""
    return parts[-1]


def search(llm: Any, row: conn.StoredRow) -> Seen | None:
    """None when the search did not return the person's own profile."""
    from jobengine.llm import LLMError

    p = row.person
    if not p.url or not conn.person_key(p.url, p.name, p.company).startswith("in/"):
        return None
    user = (f"Person: {p.name}\nCompany: {p.company or 'not given'}\n"
            f"Position: {p.position or 'not given'}")
    titles: dict[str, str] = {}
    try:
        raw, sources = llm.complete_json_with_search(STAGE, SYSTEM, user, max_tokens=400,
                                                     max_uses=2, titles=titles)
    except LLMError as exc:
        log.info("connection search failed for a person at %s: %s", p.company, exc)
        return None
    want = p.key
    own = next((u for u in sources if conn.person_key(u, "", "") == want), None)
    if own is None:
        return None  # the profile was not among the results: nothing is trusted
    raw = raw or {}
    host = (urlsplit(own).hostname or "").lower()
    country = linkedin_country(host) or country_of(str(raw.get("location") or "")) or ""
    shown = titles.get(own) or ""
    title = shown if "linkedin" in shown.casefold() and " - " in shown else str(
        raw.get("title") or "")
    company = title_company(title, p.name)
    other = company if company and not conn.same_company(company, p.company) else ""
    return Seen(country=country, title=title[:200], other_company=other[:80])


# ---------------------------------------------------------------- bot_state


def _load(state: Any, today: date) -> dict[str, Any]:
    value = dict((state.get(KEY) if state is not None else None) or {})
    cutoff = (today - timedelta(days=RECHECK_DAYS)).isoformat()
    value["people"] = {k: v for k, v in (value.get("people") or {}).items()
                       if str((v or {}).get("date") or "") > cutoff}
    return value


def may_have_left(state: Any, today: date) -> dict[str, str]:
    """person key -> the other company the search showed (for connections.block)."""
    people = _load(state, today)["people"]
    return {k: str(v.get("left")) for k, v in people.items() if v.get("left")}


def wanted(row: conn.StoredRow, searched: dict[str, Any]) -> bool:
    return (not row.removed and bool(row.person.url) and not row.info.get("Country")
            and not is_junior(row.person.position)
            and conn.kind(row.person.position) != conn.OTHER_KIND
            and row.person.key not in searched)


def run(store: conn.ConnectionsStore, llm: Any, rows: Iterable[conn.StoredRow], state: Any,
        today: date, limit: int) -> Outcome:
    """Search each wanted row (at most `limit`) and fill an empty Country."""
    value = _load(state, today)
    people = value["people"]
    out = Outcome()
    for row in rows:
        if out.searched >= limit:
            break
        if not wanted(row, people):
            continue
        out.searched += 1
        seen = search(llm, row)
        entry: dict[str, Any] = {"date": today.isoformat()}
        if seen is not None:
            if seen.other_company:
                entry["left"] = seen.other_company
                out.left.append(f"{row.person.name} (now {seen.other_company})")
            if seen.country:
                store.update(row.page_id, {"Country": ("rich_text", seen.country),
                                           "Location source": ("select", SOURCE)})
                out.placed.append(f"{row.person.name} ({seen.country})")
        people[row.person.key] = entry
    if state is not None:
        state.set(KEY, value)
    return out


def summary(out: Outcome, where: str) -> str:
    lines = [f"🔎 Google search: {out.searched} of your connections {where} looked up, "
             f"{len(out.placed)} now have a country (no LinkedIn page opened, no email "
             "guessed)."]
    lines += [f"- {name}" for name in out.placed[:10]]
    if out.left:
        lines.append("May have left (the search shows another company):")
        lines += [f"- {name}" for name in out.left[:10]]
    return "\n".join(lines)


def daily_due(state: Any, now: datetime) -> bool:
    value = (state.get(KEY) if state is not None else None) or {}
    return now.hour >= DAILY_HOUR and value.get("day") != now.date().isoformat()


def mark_day(state: Any, today: date) -> None:
    value = _load(state, today)
    value["day"] = today.isoformat()
    state.set(KEY, value)
