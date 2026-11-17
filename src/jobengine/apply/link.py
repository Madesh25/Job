"""The link to apply on: the employer's own page instead of an Adzuna link.

Adzuna shows "this job is not available in your region" when its link is opened outside the
job's country. Its /land/ad/<id> link redirects to the employer's page, which has no such
block, so when you approve a resume the bot follows it once (http.get_page: plain GET,
public hosts only) and gives you the employer's link. When that does not work (Adzuna
refuses the request from where the bot runs, or the redirect stays on Adzuna) you get the
Adzuna link, a web search for the job on the company's own site, and a note that a VPN set
to the job's country opens the Adzuna link.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import quote_plus, urlsplit

from bs4 import BeautifulSoup

from jobengine import http
from jobengine.sweep.fulltext import employer_link

log = logging.getLogger("jobengine.apply")

PageGetter = Callable[[str], tuple[str, str]]  # url -> (final url, html)


@dataclass(frozen=True)
class Link:
    url: str  # the link to show
    employer: bool = False  # True: the employer's page found through Adzuna's redirect
    adzuna: str | None = None  # the Adzuna link it replaced, or the one still shown


def is_adzuna(url: str) -> bool:
    return "adzuna." in (urlsplit(url).hostname or "")


def land_link(url: str) -> str | None:
    """Adzuna's redirect to the employer for a details or land link, else None."""
    if not is_adzuna(url):
        return None
    if "/land/ad/" in urlsplit(url).path:
        return url
    return employer_link(url)


def resolve(url: str, get_page: PageGetter) -> Link:
    land = land_link(url)
    if land is None:
        return Link(url=url)
    try:
        final, _page = get_page(land)
    except http.HttpError as exc:
        log.info("employer link not reached for %s: %s", url, exc)
        return Link(url=url, adzuna=url)
    if is_adzuna(final):
        return Link(url=url, adzuna=url)
    return Link(url=final, employer=True, adzuna=url)


def search_link(company: str, role: str) -> str:
    return "https://www.google.com/search?q=" + quote_plus(f"{company} {role} careers")


def blocked_note(company: str, role: str, country: str) -> str:
    where = country or "the job's country"
    return (f"Adzuna may say \"not available in your region\" outside {where}. Find the job "
            f"on {company or 'the company'}'s own site: {search_link(company, role)} , or "
            f"open the Adzuna link with a VPN set to {where}.")


# ---------------------------------------------------------------- is the posting still open?
# Phase 6 (8 Oct): a resume was built for a job whose page answered 404. Before a resume is
# built, the job link is opened once (plain GET, never LinkedIn or a blocked site) and these
# signs of a closed posting are looked for. Anything unclear counts as open: the check only
# warns, you decide.

GONE = (404, 410)
CLOSED_WORDS = (
    "no longer available", "no longer accepting applications", "no longer accepting "
    "candidates", "this job has expired", "job has expired", "posting has expired",
    "job posting has expired", "position has been filled", "this position is filled",
    "this job is closed", "job is no longer", "vacancy is closed", "vacancy has been closed",
    "this vacancy is no longer", "job not found", "this job doesn't exist",
    "this job does not exist", "applications are closed",
    # Dutch and Polish job sites
    "vacature is gesloten", "vacature is niet meer beschikbaar", "vacature is verlopen",
    "oferta wygasła", "ogłoszenie wygasło", "oferta jest nieaktualna",
    "ogłoszenie jest nieaktualne", "oferta została zakończona",
)
# Greenhouse sends a closed job to the board with ?error=true.
CLOSED_URL = re.compile(r"[?&]error=true\b")
LD_JSON = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)


def _valid_through(page: str) -> date | None:
    """validThrough of a schema.org JobPosting on the page, if any."""
    for raw in LD_JSON.findall(page):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("validThrough"):
                try:
                    return datetime.fromisoformat(
                        str(item["validThrough"]).replace("Z", "+00:00")).date()
                except ValueError:
                    return None
    return None


HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
# 10 Nov (Phase 23 3-08): eBay's open Cloud SRE job was marked Expired because its page also
# carries the "no longer available" message the site shows when a job closes, hidden. An open
# job page asks you to apply; a closed one does not.
APPLY_WORDS = ("apply now", "apply for this job", "apply for this position", "apply online",
               "submit application", "solliciteer nu", "solliciteer direct", "aplikuj teraz",
               "aplikuj")


def _hidden(tag: Any) -> bool:
    attrs = getattr(tag, "attrs", None) or {}
    return ("hidden" in attrs or str(attrs.get("aria-hidden", "")).lower() == "true"
            or bool(HIDDEN_STYLE.search(str(attrs.get("style", "")))))


def visible_text(page: str) -> str:
    soup = BeautifulSoup(page, "html.parser")
    for tag in soup.find_all(("script", "style", "noscript", "template")):
        tag.decompose()
    for tag in [t for t in soup.find_all(True) if _hidden(t)]:
        if not tag.decomposed:
            tag.decompose()
    return " ".join(soup.get_text(" ").split()).lower()


def _open_posting(page: str) -> bool:
    """A schema.org JobPosting on the page (its end date was checked before)."""
    return any('"jobposting"' in raw.lower().replace(" ", "") for raw in LD_JSON.findall(page))


def closed_reason(url: str, get_page: PageGetter, today: date) -> str | None:
    """Why the posting looks closed, or None (open, or it could not be checked)."""
    host = (urlsplit(url).hostname or "").lower()
    if not url.lower().startswith("https://") or "linkedin" in host:
        return None
    try:
        final, page = get_page(url)
    except http.HttpError as exc:
        if exc.status in GONE:
            return f"the job page answers HTTP {exc.status} (not found)"
        log.info("open check: %s not checked: %s", host, exc)
        return None
    if CLOSED_URL.search(final):
        return "the job link now opens the company's job list"
    ends = _valid_through(page)
    if ends is not None and ends < today:
        return f"the job page says it closed on {ends.isoformat()}"
    text = visible_text(page)
    for words in CLOSED_WORDS:
        if words in text:
            if _open_posting(page) or any(w in text for w in APPLY_WORDS):
                log.info("open check: %s says %r but still offers to apply: open", host, words)
                return None
            return f'the job page says "{words}"'
    return None
