"""More about your LinkedIn connections and contacts, from a Google search (28 Oct, your
request: use the Google Cloud credit to enrich connections and contacts).

The LinkedIn export has no location and no work email, so most connections are "location
unknown" (all 14 at ING on 28 Oct) and Hunter's people have no location either. The bot
never opens LinkedIn. Instead the AI (Config model.people: Gemini with Google Search, or
Claude with web search) searches for the person, the way the contact search reads LinkedIn
result titles (contacts/web_search.py), and the bot checks every answer:

- The person: a LinkedIn result the search really returned. For a connection it must be
  their own profile link from your export; for a contact (no link known) the result's title
  must start with their name and name the company. A connection without a profile link is
  never searched.
- Country: the result's address when it is a country site (nl., pl. or ie.linkedin.com), else
  the place the result shows when it names Poland, the Netherlands or Ireland or one of their
  cities.
- Current job: the result's title. When it names another company the connection is shown as
  "may have left" under a job.
- Work email: only an address the bot itself read on a public page the search returned
  (a team page, a talk, GitHub; never LinkedIn): the exact address must be on that page, on
  the company's email domain, and not a personal one. It is never guessed. Otherwise work
  emails still come from Prospeo and Hunter.

Connections (the 🤝 LinkedIn Connections table): an empty Country is filled (Location source
"AI search"), an empty Work email gets a found address, and an empty Notes gets one line with
what the search showed. Your own notes are never changed. When: the "Location and email"
button (MAX_ON_DEMAND people at that company) and once a day from DAILY_HOUR for the
companies of your approved jobs (MAX_DAILY people); a person is searched again after
RECHECK_DAYS. Contacts: contacts/finder.py asks place() for people a provider found without a
location (the strict rule), after Prospeo.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from jobengine.contacts import connections as conn
from jobengine.contacts.classify import is_junior, is_personal, on_domain, valid_email
from jobengine.contacts.providers.github import country_of
from jobengine.contacts.web_search import linkedin_country

log = logging.getLogger("jobengine.contacts")

STAGE = "people"  # the contact search's model (Config model.people)
KEY = "connections.searched"  # bot_state: {"people": {person key: {...}}, "day": iso}
RECHECK_DAYS = 30
MAX_ON_DEMAND = 6
MAX_DAILY = 20
MAX_CONTACTS = 3  # contacts placed by a search per job (finder.py)
DAILY_HOUR = 13  # your local time: after the morning runs
JOB_STATUSES = ("Approved", "Resume built", "Applied")
SOURCE = "AI search"  # an existing Location source option
TITLE_TAIL = re.compile(r"\s*[|\u00b7]\s*linkedin\s*$", re.I)
TITLE_PARTS = re.compile(r"\s+[-\u2013\u2014]\s+")
EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

PageReader = Callable[[str], tuple[str, str]]  # url -> (final url, html)

SYSTEM = """You find public facts about one person who works at a company.
Search the web, for example: "First Last" Company site:linkedin.com/in, and "First Last"
Company email.
Answer with JSON only:
{"url": "<their LinkedIn profile link exactly as the search returned it, or empty>",
 "title": "<that search result's title as shown, e.g. Anna Lis - Platform Engineer - Acme>",
 "location": "<the place the search result shows, or empty>",
 "email": "<a work email address written on a public page you found, or empty>",
 "email_page": "<the link of the page where that address is written, or empty>"}
Only this exact person (same first and last name). Use only what the search results show.
Never make up or guess an email address. When you are not sure, leave the field empty."""


@dataclass
class Seen:
    """What one search showed about a person, after the bot's checks."""

    profile: str = ""  # their LinkedIn result link
    country: str = ""
    title: str = ""
    location: str = ""  # the place shown, as written
    other_company: str = ""  # the title names another company: may have left
    email: str = ""  # read by the bot on email_page
    email_page: str = ""

    def note(self, today: date) -> str:
        parts = [p for p in (self.title, self.location) if p]
        if self.email:
            parts.append(f"email seen on {self.email_page}")
        return f"Google search {today:%d %b}: " + "; ".join(parts) if parts else ""


@dataclass
class Outcome:
    searched: int = 0
    placed: list[str] = field(default_factory=list)  # "Anna Lis (Netherlands)"
    emails: list[str] = field(default_factory=list)  # "Anna Lis (anna@acme.com)"
    left: list[str] = field(default_factory=list)  # "Anna Lis (now Acme)"


# ---------------------------------------------------------------- one person


def title_company(title: str, name: str) -> str:
    """"Anna Lis - Platform Engineer - Acme | LinkedIn" -> "Acme" (the last part), only when
    the title starts with the person's name."""
    parts = [p.strip() for p in TITLE_PARTS.split(TITLE_TAIL.sub("", title))]
    if len(parts) < 3 or not conn.same_person(parts[0], name):
        return ""
    return parts[-1]


def _profile(sources: list[str], profile_url: str, name: str, company: str,
             title: str) -> str:
    """The person's LinkedIn result among the search's links: their own export link for a
    connection; for a contact, a profile link whose title names them and the company."""
    links = [u for u in sources if conn.person_key(u, "", "").startswith("in/")]
    if profile_url:
        want = conn.person_key(profile_url, "", "")
        return next((u for u in links if conn.person_key(u, "", "") == want), "")
    shown = title_company(title, name)
    if links and shown and conn.same_company(shown, company):
        return links[0] if len(links) == 1 else ""
    return ""


def own_domain(page: str, company: str) -> str:
    """The company's own site as its email domain: careers.acme.com -> acme.com when the
    company's first word is in it ("" otherwise)."""
    host = (urlsplit(page).hostname or "").lower()
    base = ".".join(host.split(".")[-2:])
    word = conn.company_word(company)
    return base if word and word in base.split(".")[0] else ""


def _email(raw: dict[str, Any], sources: list[str], domain: str, company: str,
           read_page: PageReader | None) -> tuple[str, str]:
    """(address, page) when the bot read that exact address on a page the search returned,
    on the company's domain (Target Companies Domain, else the company's own site); else
    ("", "")."""
    from jobengine import http

    email = str(raw.get("email") or "").strip().lower()
    page = str(raw.get("email_page") or "").strip()
    domain = domain or own_domain(page, company)
    if (read_page is None or not domain or not valid_email(email) or is_personal(email)
            or not on_domain(email, domain)
            or page.rstrip("/") not in {u.rstrip("/") for u in sources}
            or "linkedin" in (urlsplit(page).hostname or "")):
        return "", ""
    try:
        _final, html = read_page(page)
    except http.HttpError as exc:
        log.info("email page not read %s: %s", urlsplit(page).hostname, exc)
        return "", ""
    found = {m.group(0).lower().rstrip(".") for m in EMAIL_IN_TEXT.finditer(html)}
    return (email, page) if email in found else ("", "")


def search(llm: Any, name: str, company: str, position: str = "", profile_url: str = "",
           domain: str = "", read_page: PageReader | None = None) -> Seen | None:
    """None when the search did not return the person (nothing it said is trusted)."""
    from jobengine.llm import LLMError

    user = (f"Person: {name}\nCompany: {company or 'not given'}\n"
            f"Position: {position or 'not given'}")
    titles: dict[str, str] = {}
    try:
        raw, sources = llm.complete_json_with_search(STAGE, SYSTEM, user, max_tokens=500,
                                                     max_uses=3, titles=titles)
    except LLMError as exc:
        log.info("person search failed at %s: %s", company, exc)
        return None
    raw = raw or {}
    answered = str(raw.get("title") or "")
    own = _profile(sources, profile_url, name, company, answered)
    if not own:
        return None
    shown = titles.get(own) or ""
    title = shown if "linkedin" in shown.casefold() and " - " in shown else answered
    location = str(raw.get("location") or "").strip()
    host = (urlsplit(own).hostname or "").lower()
    country = linkedin_country(host) or country_of(location) or ""
    now_at = title_company(title, name)
    other = now_at if now_at and not conn.same_company(now_at, company) else ""
    email, page = _email(raw, sources, domain, company, read_page) if not other else ("", "")
    return Seen(profile=own, country=country, title=TITLE_TAIL.sub("", title)[:150],
                location=location[:80], other_company=other[:80], email=email,
                email_page=page)


# ---------------------------------------------------------------- connections


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
    """A connection worth a search: a profile link, something missing, not searched lately."""
    missing = not row.info.get("Country") or not row.info.get("Work email")
    return (not row.removed and bool(row.person.url) and missing
            and not is_junior(row.person.position)
            and conn.kind(row.person.position) != conn.OTHER_KIND
            and row.person.key not in searched)


def run(store: conn.ConnectionsStore, llm: Any, rows: Iterable[conn.StoredRow], state: Any,
        today: date, limit: int, domain_of: Callable[[str], str | None] = lambda c: None,
        read_page: PageReader | None = None) -> Outcome:
    """Search each wanted row (at most `limit`) and fill its empty columns."""
    value = _load(state, today)
    people = value["people"]
    out = Outcome()
    for row in rows:
        if out.searched >= limit:
            break
        if not wanted(row, people):
            continue
        out.searched += 1
        p = row.person
        seen = search(llm, p.name, p.company, p.position, p.url,
                      domain_of(p.company) or "", read_page)
        entry: dict[str, Any] = {"date": today.isoformat()}
        if seen is not None:
            props: dict[str, Any] = {}
            if seen.other_company:
                entry["left"] = seen.other_company
                out.left.append(f"{p.name} (now {seen.other_company})")
            if seen.country and not row.info.get("Country"):
                props["Country"] = ("rich_text", seen.country)
                props["Location source"] = ("select", SOURCE)
                out.placed.append(f"{p.name} ({seen.country})")
            if seen.email and not row.info.get("Work email"):
                props["Work email"] = ("email", seen.email)
                out.emails.append(f"{p.name} ({seen.email})")
            note = seen.note(today)
            if note and not row.info.get("Notes"):
                props["Notes"] = ("rich_text", note[:500])
            if props:
                store.update(row.page_id, props)
        people[p.key] = entry
    if state is not None:
        state.set(KEY, value)
    return out


def summary(out: Outcome, where: str) -> str:
    lines = [f"🔎 Google search: {out.searched} of your connections {where} looked up: "
             f"{len(out.placed)} now have a country, {len(out.emails)} a work email found on a "
             "public page (no LinkedIn page opened, no email guessed)."]
    lines += [f"- {name}" for name in out.placed[:10]]
    lines += [f"- {name}" for name in out.emails[:10]]
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


# ---------------------------------------------------------------- contacts


def place(llm: Any, name: str, company: str, title: str, country: str) -> Seen | None:
    """A contact a provider found without a location (finder.py, strict rule): their
    LinkedIn result places them in the job's country, or None."""
    seen = search(llm, name, company, title)
    if seen is None or seen.other_company or seen.country.casefold() != country.casefold():
        return None
    return seen
