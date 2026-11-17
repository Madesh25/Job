"""Find a company's email domain without asking you (22 Oct, your request in Phase 11 O-03).

In this order, the first that is sure wins:
1. The job's own pages: the job link and careers page when they are on the company's site,
   emails in the description (finder.domain_options). Sure when exactly one of them carries
   a word of the company name.
2. An AI web search (Config model.people, the contact search's model): "what is the email
   domain of <company>". Sure only when a search result really lives on that domain and the
   domain or that result's title carries the company name, and it is no job board, ATS or
   personal mail domain.
A sure domain is saved: Target Companies Domain in prod (only that column, only when it is
empty), and the bot's memory everywhere, so the next job of the company needs no search.
3. The company's website by its name (7 Nov, Phase 22 REQ-01: the bot asked for BNP Paribas
   Bank Polska's domain): the likely addresses (bnpparibas.pl, bnpparibas.com, ...) are
   opened, and one counts only when its page names every distinctive word of the company.
Each step is written to the log (what was tried, what was found, where it was saved).
When nothing is sure, the bot asks you as before (the last fallback). Emails are never
guessed from a domain: the finders still give them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from jobengine.llm import LLMError
from jobengine.sweep.normalize import canon_company, without_region

log = logging.getLogger("jobengine.contacts")

STAGE = "people"
MAX_SEARCHES = 3
SYSTEM_PROMPT = """You find a company's email domain: the part after @ in its employees'
work email addresses (for example acme.com for anna@acme.com). Search the web for the
company's official website.

Return one JSON object only:
{"domain": <the email domain, or "" when you are not sure>,
 "sources": [<URLs from your search results on that domain or naming it>]}

Rules:
- The company's own domain, never a job board, recruiting software (workday, greenhouse,
  lever, smartrecruiters), LinkedIn or a personal mail domain.
- A big company with country sites (acme.pl, acme.nl) usually mails from its main domain;
  give the main domain unless the search shows the country's own one is used for email.
"""


# Words too common to tell companies apart ("Acme Solutions" must match acme, not solutions).
COMMON = frozenset({"group", "solutions", "technologies", "technology", "services", "global",
                    "international", "systems", "software", "digital", "consulting", "company",
                    "the", "and", "holding", "europe", "bank", "labs", "data"})


# Country words: a Polish arm is "<name> Polska", its site is <name>.pl.
COUNTRY_WORDS = frozenset({"polska", "poland", "nederland", "netherlands", "ireland", "irish",
                           "dutch", "europe", "emea"})
TLDS = {"Poland": "pl", "Netherlands": "nl", "Ireland": "ie"}
MAX_GUESSES = 6


def company_words(company: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", canon_company(without_region(company)).lower())
            if len(w) > 2 and w not in COMMON]


def names(text: str, company: str) -> bool:
    words = company_words(company)
    squashed = re.sub(r"[^a-z0-9]", "", text.lower())
    return bool(words) and ("".join(words) in squashed or any(w in squashed for w in words))


def from_job_pages(company: str, options: list[str]) -> str | None:
    """The one domain from the job's own pages that carries the company name."""
    named = [d for d in options if names(d.split(".")[0], company)]
    return named[0] if len(named) == 1 else None


def from_ai(llm: Any, company: str, country: str,
            valid: Callable[[str, str], str | None], base: Callable[[str], str],
            not_company: Callable[[str], bool]) -> tuple[str | None, str]:
    """(domain or None, note). `valid`, `base` and `not_company` are the finder's own
    domain rules, so a found domain obeys exactly what a typed one must."""
    titles: dict[str, str] = {}
    try:
        raw, urls = llm.complete_json_with_search(
            STAGE, SYSTEM_PROMPT,
            f"COMPANY: {without_region(company)}\nCOUNTRY OF THE JOB: {country}",
            max_tokens=400, max_uses=MAX_SEARCHES, key=f"domain-{canon_company(company)}",
            titles=titles)
    except LLMError as exc:
        return None, f"AI domain search: {exc}"
    domain = valid(str((raw or {}).get("domain") or ""), company) if isinstance(raw, dict) \
        else None
    if not domain or not_company(domain):
        return None, "AI domain search: no sure answer"
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if not host or base(host) != base(domain):
            continue
        if names(domain.split(".")[0], company) or names(titles.get(url, ""), company):
            return domain, f"domain {domain} found by AI web search ({host})"
    return None, f"AI domain search: {domain} not confirmed by a search result"


def guesses(company: str, country: str) -> list[str]:
    """Likely website addresses from the name: all its words, the first two, the first one;
    the job's country domain first, then .com."""
    words = [w for w in company_words(company) if w not in COUNTRY_WORDS]
    if not words:
        return []
    stems = list(dict.fromkeys(["".join(words), "".join(words[:2]), words[0]]))
    tlds = [tld for tld in (TLDS.get(country or ""), "com") if tld]
    return [f"{stem}.{tld}" for tld in tlds for stem in stems][:MAX_GUESSES]


def from_site_guess(company: str, country: str, read_page: Callable[[str], str],
                    valid: Callable[[str, str], str | None],
                    not_company: Callable[[str], bool]) -> tuple[str | None, str]:
    """(domain or None, note): the first likely address whose home page names every
    distinctive word of the company (no AI, a few page reads)."""
    words = [w for w in company_words(company) if w not in COUNTRY_WORDS]
    wrong: list[str] = []  # read, but the page does not name the company
    silent: list[str] = []  # did not answer
    for guess in guesses(company, country):
        domain = valid(guess, company)
        if not domain or not_company(domain):
            continue
        try:
            text = read_page(f"https://{domain}/") or ""
        except Exception as exc:  # a site that does not answer is just not this one
            log.info("domain guess %s for %s: not read (%s)", domain, company, exc)
            silent.append(domain)
            continue
        lower = text.lower()  # whole words: "ing" never matches "banking"
        if words and all(re.search(rf"\b{re.escape(w)}\b", lower) for w in words):
            log.info("domain guess %s for %s: its page names the company", domain, company)
            return domain, f"domain {domain} found on the company's website"
        log.info("domain guess %s for %s: page does not name the company", domain, company)
        wrong.append(domain)
    parts = [f"{', '.join(wrong)} did not name the company"] if wrong else []
    parts += [f"{', '.join(silent)} did not answer"] if silent else []
    return None, "website check: " + ("; ".join(parts) or "no likely address")
