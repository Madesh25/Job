"""Email addresses written on the company's own website (27 Oct, your request: free contacts).

Before any paid lookup the bot reads the company's home page, then at most MAX_PAGES of its
own pages whose link names careers, jobs or contact (English, Polish and Dutch words). It keeps
only addresses written there on the company's domain, never a guessed one:

- a hiring mailbox (careers@, jobs@, hr@, rekrutacja@, werkenbij@ ...) is kept like a mailbox
  in a job posting (Type Other, it gets the mail for mailboxes);
- a named person's address only from a careers or jobs page, or when the line around it says
  recruiting, HR or talent: a Recruiter/TA contact.

Sales, support and info@ addresses are left out. Each company's result is kept in Bot State
for CACHE_DAYS, so its next jobs read nothing. Plain GET like every job page (no login, never
LinkedIn); a page that cannot be read is skipped. Pure apart from `read`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from jobengine.contacts import jd_emails

SWITCH = "contacts.site"  # Notion Config: off stops it (default on)
CACHE_KEY = "site_emails:{company}"  # bot_state: {"date", "found": [[email, name, generic]]}
CACHE_DAYS = 30
MAX_PAGES = 4
CAREERS_WORDS = r"career|jobs?\b|join|work-with-us|werken|vacature|kariera|praca|rekrutacja|" \
                r"hiring|talent"
CONTACT_WORDS = r"contact|kontakt|about|over-ons|o-nas|team"
HIRING_MAILBOXES = (*jd_emails.GENERIC_MAILBOXES, "career", "job", "recruiting", "recruiter",
                    "hiring", "kariera", "werkenbij", "werken", "vacatures", "vacature",
                    "people", "join", "talentacquisition", "ta")
NOT_PEOPLE = ("info", "sales", "support", "contact", "office", "hello", "privacy", "press",
              "marketing", "media", "billing", "invoice", "admin", "noreply", "no-reply",
              "gdpr", "dpo", "legal", "biuro", "kontakt", "service")
HIRING_LINE = re.compile(r"recruit|talent|\bhr\b|human resources|people|career|hiring|"
                         r"rekrut|kadr|werving", re.I)


@dataclass(frozen=True)
class SiteEmail:
    email: str
    name: str | None
    generic: bool
    url: str


def is_on(config: Any) -> bool:
    return (config.get(SWITCH) or "on").strip().lower() != "off"


def on_domain(email: str, domain: str) -> bool:
    host = email.rsplit("@", 1)[-1].lower()
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def _same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return urlsplit(url).scheme == "https" and (host == domain or host.endswith("." + domain))


def links(html: str, base: str, domain: str) -> list[str]:
    """The site's own careers pages first, then its contact pages, at most MAX_PAGES."""
    soup = BeautifulSoup(html or "", "html.parser")
    careers: list[str] = []
    contact: list[str] = []
    for a in soup.find_all("a", href=True):
        url = urljoin(base, str(a["href"])).split("#", 1)[0]
        if not _same_site(url, domain) or url.rstrip("/") == base.rstrip("/"):
            continue
        words = f"{urlsplit(url).path} {a.get_text(' ')}".lower()
        if re.search(CAREERS_WORDS, words):
            careers.append(url)
        elif re.search(CONTACT_WORDS, words):
            contact.append(url)
    out: list[str] = []
    for url in [*careers, *contact]:
        if url not in out:
            out.append(url)
    return out[:MAX_PAGES]


def page_text(html: str) -> str:
    """The visible text, one line per block, plus each mailto address on its own line."""
    soup = BeautifulSoup(html or "", "html.parser")
    mails = [str(a["href"])[7:].split("?", 1)[0] for a in soup.find_all("a", href=True)
             if str(a["href"]).lower().startswith("mailto:")]
    for tag in soup.find_all(("script", "style", "noscript", "template")):
        tag.decompose()
    text = soup.get_text("\n")
    return "\n".join([*(" ".join(line.split()) for line in text.splitlines() if line.strip()),
                      *mails])


def found_on(html: str, url: str, domain: str, personal: Iterable[str],
             careers_page: bool) -> list[SiteEmail]:
    text = page_text(html)
    lines = text.splitlines()
    out = []
    for item in jd_emails.extract(text, personal, HIRING_MAILBOXES):
        if not on_domain(item.email, domain):
            continue
        if item.generic:
            out.append(SiteEmail(item.email, item.email, True, url))
            continue
        local = item.email.split("@", 1)[0].lower()
        if not item.name or local in NOT_PEOPLE:
            continue  # only a named person, never info@ or sales@
        around = " ".join(line for line in lines if item.email in line or local in line.lower())
        if careers_page or HIRING_LINE.search(around):
            out.append(SiteEmail(item.email, item.name, False, url))
    return out


def search(domain: str, read: Callable[[str], str], personal: Iterable[str],
           start: Iterable[str | None] = ()) -> tuple[list[SiteEmail], list[str]]:
    """(addresses, pages read). `start`: more pages to read first (the careers URL from
    Target Companies), kept only when on the company's domain."""
    home = f"https://{domain}/"
    pages: list[tuple[str, bool]] = [(u, True) for u in start if u and _same_site(u, domain)]
    read_urls: list[str] = []
    found: list[SiteEmail] = []
    try:
        html = read(home)
        read_urls.append(home)
        found += found_on(html, home, domain, personal, False)
        for url in links(html, home, domain):
            pages.append((url, bool(re.search(CAREERS_WORDS, url.lower()))))
    except Exception:  # an unreadable home page: the careers URL may still work
        pass
    for url, careers in pages:
        if url in read_urls or len(read_urls) > MAX_PAGES:
            continue
        try:
            html = read(url)
        except Exception:
            continue
        read_urls.append(url)
        found += found_on(html, url, domain, personal, careers)
    unique: dict[str, SiteEmail] = {}
    for item in found:
        unique.setdefault(item.email.lower(), item)
    return list(unique.values()), read_urls


def cached(state: Any, company: str, today: date) -> list[SiteEmail] | None:
    value = (state.get(CACHE_KEY.format(company=company)) if state is not None else None) or {}
    try:
        when = date.fromisoformat(str(value.get("date") or ""))
    except ValueError:
        return None
    if today - when > timedelta(days=CACHE_DAYS):
        return None
    return [SiteEmail(e, n or None, bool(g), u) for e, n, g, u in value.get("found") or []]


def remember(state: Any, company: str, today: date, found: list[SiteEmail]) -> None:
    if state is not None:
        state.set(CACHE_KEY.format(company=company),
                  {"date": today.isoformat(),
                   "found": [[f.email, f.name or "", f.generic, f.url] for f in found]})
