"""AI web search for contacts (20 Oct): the last step, when the providers left slots open.

1. The AI (Config model.people, a model with web search) searches the company's public pages
   for people who work there in the job's country: team pages, engineering blogs,
   conference and meetup speaker pages, press releases, GitHub. Never LinkedIn.
2. Every name is checked: the page must be one the search really returned (not LinkedIn,
   not a site that blocks the bot), and the bot reads that page once to see the name and
   the company on it. The person counts as in the country only when the page names the
   country or one of its cities.
3. The email is never guessed: Hunter's email finder (name + company domain, 1 credit when
   it finds one), else Prospeo's enrich by name (1 credit). An address Hunter calls invalid
   is dropped. The contact is saved with that provider as Source and the page in Notes.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from jobengine import http
from jobengine.contacts.models import Candidate, ProviderResult
from jobengine.contacts.providers.base import ProviderDeps
from jobengine.contacts.providers.github import COUNTRY_WORDS
from jobengine.llm import LLMError
from jobengine.strategy.validate import norm_url

log = logging.getLogger("jobengine.contacts")

STAGE = "people"
SWITCH = "contacts.ai_search"  # Notion Config: "on" runs this step
MAX_PEOPLE = 6  # names asked of the AI
MAX_LOOKUPS = 4  # email look-ups per job (1 credit each when found)
MAX_SEARCHES = 5  # web searches per job (Claude; Gemini decides itself)
MAX_TOKENS = 1500
HUNTER_FINDER = "https://api.hunter.io/v2/email-finder"
PROSPEO_ENRICH = "https://api.prospeo.io/enrich-person"
SLOT_ROLES = {
    "Peer engineer": "DevOps, SRE, platform or cloud engineers",
    "Hiring": "engineering managers, heads or directors of engineering, infrastructure or "
              "platform",
    "Recruiter/TA": "recruiters, talent acquisition or HR people",
}

SYSTEM_PROMPT = """You find people who work at one company in one country, for a job seeker
who wants to write to them. Search the web for the company's public pages: its own team and
about pages, its engineering blog, conference and meetup speaker pages, press releases and
public GitHub pages. Never use LinkedIn or any social network page.

Return one JSON object only:
{"people": [{"name": <full name>, "title": <their job title at the company>,
 "country": <the country where they work, or "unknown">,
 "sources": [<the URL of the page from your search results that names them>],
 "quote": <the words on that page that name them and their role, at most 20 words>}]}

Rules:
- At most MAX_PEOPLE people, the roles asked for first.
- Only people who work at this company now, named on a page your searches returned. Never
  guess a name and never write an email address.
- If you find nobody, return {"people": []}.
"""


@dataclass
class Person:
    name: str
    title: str
    country: str
    sources: list[str] = field(default_factory=list)
    page: str = ""  # the page the name was checked on
    placed: bool = False  # the page names the job's country or one of its cities


def user_prompt(company: str, domain: str, country: str, open_slots: Mapping[str, int]) -> str:
    roles = "; ".join(f"{n} {SLOT_ROLES.get(k, k)}" for k, n in open_slots.items() if n > 0)
    return (f"COMPANY: {company}\nCOMPANY WEBSITE: {domain}\nCOUNTRY: {country}\n"
            f"PEOPLE WANTED: {roles}")


def parse_people(raw: Any) -> list[Person]:
    items = raw.get("people") if isinstance(raw, dict) else None
    people = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())
        if len(name.split()) < 2 or "@" in name:
            continue  # a first name alone cannot be looked up
        sources = item.get("sources") or []
        sources = [sources] if isinstance(sources, str) else sources
        people.append(Person(name=name, title=" ".join(str(item.get("title") or "").split()),
                             country=str(item.get("country") or "unknown"),
                             sources=[str(u).strip() for u in sources if str(u).strip()]))
    return people[:MAX_PEOPLE]


def _plain(text: str) -> str:
    """Lower case without accents, single spaces: "Łukasz Wójcik" -> "lukasz wojcik"."""
    text = text.replace("ł", "l").replace("Ł", "L")
    decomposed = unicodedata.normalize("NFKD", text)
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c))
                    .casefold().split())


def _words_in(words: list[str], text: str) -> bool:
    return all(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text) for w in words if w)


def names_company(text: str, company: str, domain: str) -> bool:
    label = domain.split(".")[0]
    return _plain(company) in text or (len(label) > 3 and label in text.replace(" ", ""))


def placed_in(text: str, country: str) -> bool:
    words = COUNTRY_WORDS.get(country, ())
    return any(re.search(rf"(?<!\w){re.escape(_plain(w))}(?!\w)", text) for w in words)


def check(people: list[Person], search_urls: list[str], company: str, domain: str,
          country: str, read_page: Callable[[str], str], blocked: tuple[str, ...] = ()
          ) -> tuple[list[Person], list[str]]:
    """The people whose name and company are on a page the search returned. Notes say why
    the others were dropped (counts only, never names)."""
    known = {norm_url(u): u for u in search_urls if norm_url(u)}
    kept: list[Person] = []
    no_page = unreadable = not_named = 0
    texts: dict[str, str | None] = {}
    for person in people:
        pages = []
        for url in person.sources:
            real = known.get(norm_url(url))
            host = (urlsplit(real or "").hostname or "").lower()
            if real and "linkedin" not in host and not any(
                    host == b or host.endswith("." + b) for b in blocked):
                pages.append(real)
        if not pages:
            no_page += 1
            continue
        found = False
        for url in pages:
            if url not in texts:
                try:
                    texts[url] = _plain(read_page(url))
                except http.HttpError as exc:
                    log.info("contact page not read: %s", exc)
                    texts[url] = None
            text = texts[url]
            if text is None:
                continue
            if _words_in(_plain(person.name).split(), text) and names_company(
                    text, company, domain):
                person.page, person.placed = url, placed_in(text, country)
                found = True
                break
        if found:
            kept.append(person)
        elif all(texts.get(u) is None for u in pages):
            unreadable += 1
        else:
            not_named += 1
    notes = []
    if no_page:
        notes.append(f"{no_page} without a page from the search")
    if unreadable:
        notes.append(f"{unreadable} whose page could not be read")
    if not_named:
        notes.append(f"{not_named} not named with the company on their page")
    return kept, notes


def _split(name: str) -> tuple[str, str]:
    parts = name.split()
    return parts[0], parts[-1]


def find_email(person: Person, domain: str, hunter: ProviderDeps | None,
               prospeo: ProviderDeps | None) -> tuple[str, bool, str, int, int]:
    """(email, verified, provider, hunter credits, prospeo credits); email "" when neither
    provider knows one. Never builds an address itself."""
    first, last = _split(person.name)
    if hunter is not None and hunter.s.hunter_api_key:
        try:
            found = hunter.request("GET", HUNTER_FINDER, params={
                "domain": domain, "first_name": first, "last_name": last,
                "api_key": hunter.s.hunter_api_key})
        except http.HttpError as exc:
            log.info("hunter email finder: %s", exc)
            found = {}
        data = found.get("data") or {}
        email = str(data.get("email") or "")
        status = (data.get("verification") or {}).get("status")
        if email and status != "invalid":
            return email, status == "valid", "Hunter", 1, 0
        if email:
            return "", False, "", 1, 0  # Hunter says it bounces: never mail it
    if prospeo is not None and prospeo.s.prospeo_api_key:
        prospeo.sleep(1.5)
        body = {"data": {"first_name": first, "last_name": last, "company_website": domain},
                "only_verified_email": True, "enrich_mobile": False}
        try:
            found = prospeo.request("POST", PROSPEO_ENRICH, json_body=body,
                                    headers={"X-KEY": prospeo.s.prospeo_api_key,
                                             "Content-Type": "application/json"})
        except http.HttpError as exc:
            if "NO_MATCH" not in str(exc):
                log.info("prospeo enrich by name: %s", exc)
            return "", False, "", 0, 0
        if found.get("error"):
            return "", False, "", 0, 0
        mail = (found.get("person") or {}).get("email") or {}
        if mail.get("email"):
            return str(mail["email"]), mail.get("status") == "VERIFIED", "Prospeo", 0, 1
    return "", False, "", 0, 0


def search(llm: Any, company: str, domain: str, country: str,
           open_slots: Mapping[str, int], read_page: Callable[[str], str],
           hunter: ProviderDeps | None, prospeo: ProviderDeps | None,
           blocked: tuple[str, ...] = (), lookups: int = MAX_LOOKUPS
           ) -> tuple[ProviderResult, dict[str, int]]:
    """Candidates for the open slots and the credits spent per provider."""
    result = ProviderResult()
    spent = {"hunter": 0, "prospeo": 0}
    try:
        raw, urls = llm.complete_json_with_search(
            STAGE, SYSTEM_PROMPT.replace("MAX_PEOPLE", str(MAX_PEOPLE)),
            user_prompt(company, domain, country, open_slots), max_tokens=MAX_TOKENS,
            max_uses=MAX_SEARCHES, key=f"people-{domain}")
    except LLMError as exc:
        result.skipped_reason = f"AI search: {exc}"
        return result, spent
    people = parse_people(raw)
    kept, notes = check(people, urls, company, domain, country, read_page, blocked)
    found = 0
    for person in kept[:lookups]:
        email, verified, provider, h, p = find_email(person, domain, hunter, prospeo)
        spent["hunter"] += h
        spent["prospeo"] += p
        if not email:
            continue
        found += 1
        host = urlsplit(person.page).hostname or person.page
        result.candidates.append(Candidate(
            name=person.name, first_name=person.name.split()[0], title=person.title,
            email=email, country=country if person.placed else None,
            provider_verified=verified, source=provider,
            country_unverified=not person.placed, found_on=host))
    line = (f"AI search: {len(people)} names on public pages, {len(kept)} checked on their "
            f"page, {found} with an email from Hunter or Prospeo")
    result.notes.append(line + (f" (dropped: {'; '.join(notes)})" if notes else ""))
    return result, spent
