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

import logging
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

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
