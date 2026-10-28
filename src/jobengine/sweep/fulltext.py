"""Full job descriptions read from the posting's own page (free, no LLM, no login).

Adzuna gives only a snippet, and SmartRecruiters or Workday postings past their detail
budget come with no description at all. For the new jobs that may be saved today, the sweep
reads the posting page (http.get_page: plain GET, public https hosts only, never linkedin,
size limit) and keeps the whole description:

0. Oracle Cloud HCM jobs: the site's own public detail call (the page is a JavaScript app)
1. a schema.org JobPosting in the page's JSON-LD (most job boards and ATS pages have one)
   or the job in a site's page data (Next.js `__NEXT_DATA__`, Phenom `jobDetail`)
2. otherwise the largest block that looks like a job description (main, article, or an
   element whose id or class names a description)
3. otherwise the whole visible page text

A page that cannot be read keeps the snippet. Nothing is lost: a longer text replaces a
shorter one only, and a company site's own summary that the full text does not hold is kept
in front of it. Job links in alert emails are tracking links:
following one ends on the job's real page, whose link replaces the tracking link. LinkedIn
is never read at all.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from jobengine import http
from jobengine.parallel import run_all
from jobengine.settings import Settings
from jobengine.sweep.models import Job
from jobengine.sweep.normalize import experience, years_required

log = logging.getLogger("jobengine.sweep")

DEFAULT_MAX_PAGES = 45
DEFAULT_SOURCES = ("adzuna", "ats", "jooble", "nofluffjobs", "iamexpat", "gmail")
# Least text accepted: a JobPosting is trusted when short, a guessed block less so and
# the whole page text only when it is clearly more than a snippet.
MIN_JSON_LD = 150
MIN_BLOCK = 250
MIN_PAGE = 600
# A JobPosting shorter than this may hold only the intro: 26 Oct, Sii's had 400 characters
# and the page showed the requirements ("Minimum 5 years ...") under it.
SHORT_JSON_LD = 1000
INTRO_CHARS = 60  # the start of the JobPosting text that the visible text must contain
MAX_CHARS = 60_000
DROP_TAGS = ("script", "style", "noscript", "svg", "nav", "header", "footer", "form",
             "aside", "iframe", "button", "template")
HINT = re.compile(r"description|job[-_]?(details|posting|content|body|ad)\b|vacancy", re.I)

PageGetter = Callable[[str], tuple[str, str]]  # url -> (final url, html)
Progress = Callable[[str], None]


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    max_pages: int = DEFAULT_MAX_PAGES
    sources: tuple[str, ...] = DEFAULT_SOURCES
    full_min: int = 600
    workers: int = 8  # pages read at the same time (sweep.workers)
    # Sites that answer HTTP 403 to the bot (8 Oct: Pracuj.pl, IrishJobs, Jobs.ie): never
    # opened; their jobs wait for /jd.
    blocked_hosts: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, s: Settings) -> Config:
        cfg = s.sweep.get("fulltext") or {}
        return cls(
            enabled=bool(cfg.get("enabled", True)),
            max_pages=max(0, int(cfg.get("max_pages", DEFAULT_MAX_PAGES))),
            sources=tuple(cfg.get("sources") or DEFAULT_SOURCES),
            full_min=int(s.screening.get("full_min_chars", 600)),
            workers=max(1, int(s.sweep.get("workers", 8))),
            blocked_hosts=blocked_hosts(s),
        )


def blocked_hosts(s: Settings) -> tuple[str, ...]:
    """sweep.fulltext.blocked_hosts, lower case."""
    cfg = s.sweep.get("fulltext") or {}
    return tuple(str(h).strip().lower() for h in cfg.get("blocked_hosts") or () if str(h).strip())


# The answers of a site that refuses the bot (not a missing page: 404 is one job's problem).
REFUSED = (401, 403)


class SiteGuard:
    """A PageGetter for one sweep that skips sites refusing the bot: the configured
    blocked_hosts, and any site that answered HTTP 401 or 403 earlier in this sweep (one
    refusal, not one per job). The skip is an HttpError, like any page that cannot be read."""

    def __init__(self, get_page: PageGetter, hosts: tuple[str, ...] = ()):
        self.get_page = get_page
        self.hosts: set[str] = set(hosts)
        self.refused: set[str] = set()  # learned in this sweep
        self._lock = threading.Lock()

    def __call__(self, url: str) -> tuple[str, str]:
        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            known = (*self.hosts, *self.refused)
        if host and http.host_in(host, known):
            raise http.HttpError(f"page not read: {host} blocks the bot (alerts only)", 403,
                                 host)
        try:
            return self.get_page(url)
        except http.HttpError as exc:
            if exc.status in REFUSED and exc.host and not http.host_in(exc.host, known):
                with self._lock:
                    self.refused.add(exc.host.lower())
                log.info("%s refused the bot (HTTP %s): not opened again in this sweep",
                         exc.host, exc.status)
            raise


def _clean(text: str) -> str:
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines())
    out: list[str] = []
    for line in lines:
        if line and (not out or out[-1] != line):
            out.append(line)
    return "\n".join(out)[:MAX_CHARS]


def _html_text(value: str) -> str:
    return _clean(BeautifulSoup(value, "html.parser").get_text("\n"))


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        yield from _walk(node.get("@graph") or [])


def _is_job_posting(node: dict[str, Any]) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return "JobPosting" in kinds


def from_json_ld(soup: BeautifulSoup) -> str | None:
    """The description of a schema.org JobPosting in the page, as plain text."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        for node in _walk(data):
            if _is_job_posting(node) and isinstance(node.get("description"), str):
                parts = [node["description"]]
                for extra in ("qualifications", "responsibilities", "skills",
                              "experienceRequirements"):
                    if isinstance(node.get(extra), str):
                        parts.append(node[extra])
                text = _html_text("\n".join(parts))
                if text:
                    return text
    return None


# Keys of a Next.js page's data (`__NEXT_DATA__`) that hold job text. Pracuj.pl alert jobs
# had no description on 7 Oct although their pages opened: the text is in that data, not in
# the visible HTML or a JSON-LD JobPosting.
TEXT_KEYS = re.compile(r"description|responsibilit|requirement|qualification|duties|tasks|"
                       r"offered|benefit|textelements|plaintext|bullets", re.I)
MIN_PIECE = 30


def _texts(node: Any, wanted: bool = False) -> Iterator[str]:
    """Strings under job-text keys (and every string below such a key)."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _texts(value, wanted or bool(TEXT_KEYS.search(str(key))))
    elif isinstance(node, list):
        for item in node:
            yield from _texts(item, wanted)
    elif wanted and isinstance(node, str):
        yield node


def from_next_data(soup: BeautifulSoup) -> str | None:
    """The job text in a Next.js page's `__NEXT_DATA__` JSON, or None."""
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        return None
    try:
        data = json.loads(script.string or script.get_text() or "")
    except ValueError:
        return None
    pieces: list[str] = []
    for raw in _texts(data):
        text = _html_text(raw) if "<" in raw else _clean(raw)
        if len(text) >= MIN_PIECE and text not in pieces:
            pieces.append(text)
    joined = "\n".join(pieces)
    return joined[:MAX_CHARS] if len(joined) >= MIN_BLOCK else None


# Career sites that are JavaScript apps keep the job in data on the page (28 Oct): Phenom
# writes it under "jobDetail" in its page data (phApp.ddo). The search results gave only a
# teaser, and the visible HTML has no text.
APP_DATA_MARKERS = ('"jobDetail":',)


def from_app_data(page: str) -> str | None:
    """The job text in a career site app's page data, or None."""
    for marker in APP_DATA_MARKERS:
        start = page.find(marker)
        if start < 0:
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode(page, start + len(marker))
        except ValueError:
            continue
        pieces: list[str] = []
        for raw in _texts(data):
            text = _html_text(raw) if "<" in raw else _clean(raw)
            if len(text) >= MIN_PIECE and text not in pieces:
                pieces.append(text)
        # A teaser repeats the start of the full text: keep only pieces not inside another.
        pieces = [p for p in pieces
                  if not any(len(q) > len(p) and _flat(p)[:INTRO_CHARS] in _flat(q)
                             for q in pieces)]
        joined = "\n".join(pieces)
        if len(joined) >= MIN_BLOCK:
            return joined[:MAX_CHARS]
    return None


def _blocks(soup: BeautifulSoup) -> Iterator[Tag]:
    yield from soup.find_all(["main", "article"])
    for tag in soup.find_all(True):
        names = " ".join([tag.get("id") or "", *(tag.get("class") or [])])
        if names.strip() and HINT.search(names):
            yield tag


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


def _visible_around(page: str, intro: str) -> str | None:
    """The page's visible text when it holds the short JobPosting text and much more (the
    requirements under the intro); None otherwise."""
    soup = BeautifulSoup(page, "html.parser")
    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()
    start = _flat(intro)[:INTRO_CHARS]
    texts = [_clean(tag.get_text("\n")) for tag in _blocks(soup)]
    if soup.body is not None:
        texts.append(_clean(soup.body.get_text("\n")))
    found = [t for t in texts if len(t) >= 2 * len(intro) and start in _flat(t)]
    return min(found, key=len) if found else None


def extract(page: str) -> str | None:
    """The full job description in a page, or None when there is not enough text."""
    soup = BeautifulSoup(page, "html.parser")
    text = from_json_ld(soup)
    if text and len(text) >= MIN_JSON_LD:
        if len(text) < SHORT_JSON_LD:
            return _visible_around(page, text) or text
        return text
    data_text = from_next_data(soup) or from_app_data(page)
    if data_text:
        return data_text
    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()
    best = max((_clean(tag.get_text("\n")) for tag in _blocks(soup)), key=len, default="")
    if len(best) >= MIN_BLOCK:
        return best
    whole = _clean(soup.body.get_text("\n")) if soup.body is not None else ""
    if len(whole) >= MIN_PAGE:
        return whole
    return text or None


def needs_text(job: Job, cfg: Config) -> bool:
    """True when the job has no full description yet and its page may be read."""
    if job.source not in cfg.sources or "linkedin" in job.board.lower():
        return False
    if not job.url.lower().startswith("https://"):
        return False
    return not job.description or job.description_is_snippet or len(job.description) < cfg.full_min


ADZUNA_DETAILS = re.compile(r"^https://(www\.adzuna\.[a-z.]+)/details/(\d+)")


def employer_link(url: str) -> str | None:
    """Adzuna's own redirect to the employer's page for an Adzuna details link, else None.
    Adzuna shows "this job is not available in your region" outside the job's country; the
    employer's page has no such block."""
    match = ADZUNA_DETAILS.match(url)
    return f"https://{match.group(1)}/land/ad/{match.group(2)}" if match else None


def _is_adzuna(url: str) -> bool:
    return "adzuna." in (urlsplit(url).hostname or "")


def _open(job: Job, get_page: PageGetter) -> tuple[str, str] | None:
    """(final url, html) of the best page for the job: the employer's page when Adzuna
    redirects there, else the job's own link."""
    land = employer_link(job.url)
    if land:
        try:
            final_url, page = get_page(land)
            if not _is_adzuna(final_url):
                return final_url, page
        except http.HttpError as exc:
            log.info("employer page not reached for %s: %s", job.company, exc)
    try:
        return get_page(job.url)
    except http.HttpError as exc:
        log.info("full text not read for %s: %s", job.company, exc)
        return None


# Oracle Cloud HCM job pages are a JavaScript app: the page holds no text. Its own public
# REST call gives the whole job (P9-17, 28 Oct: the search gave only a short summary).
API_PATH = "/hcmRestApi/"  # the detail call answers JSON (the page reader asks for it)
ORACLE_JOB = re.compile(r"^https://([^/]+)/hcmUI/CandidateExperience/[a-zA-Z-]+/sites/"
                        r"([^/?#]+)/job/(\d+)")
ORACLE_PARTS = (("ExternalDescriptionStr", ""),
                ("ExternalResponsibilitiesStr", "Responsibilities"),
                ("ExternalQualificationsStr", "Qualifications"),
                ("CorporateDescriptionStr", "About the company"))


def oracle_detail_url(url: str) -> str | None:
    match = ORACLE_JOB.match(url)
    if not match:
        return None
    host, site, job_id = match.groups()
    return (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f"?expand=all&onlyData=true&finder=ById;Id=%22{job_id}%22,siteNumber={site}")


def from_api(job: Job, get_page: PageGetter) -> str | None:
    """The whole job from the site's own public detail call (Oracle), or None."""
    api = oracle_detail_url(job.url)
    if api is None:
        return None
    try:
        _url, body = get_page(api)
        data = json.loads(body)
    except (http.HttpError, ValueError) as exc:
        log.info("Oracle job details not read for %s: %s", job.company, exc)
        return None
    items = data.get("items") if isinstance(data, dict) else None
    item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
    parts = []
    for key, heading in ORACLE_PARTS:
        text = _html_text(str(item.get(key) or ""))
        if text:
            parts.append(f"{heading}\n{text}" if heading else text)
    joined = "\n\n".join(parts)
    return joined[:MAX_CHARS] if joined else None


def keep_old(old: str | None, new: str) -> str:
    """The new text, with the old one in front when the new text does not hold it: nothing
    the bot already had is lost (a summary written apart from the full text)."""
    if not old or _flat(old)[:INTRO_CHARS] in _flat(new):
        return new
    return f"{old}\n\n{new}"[:MAX_CHARS]


def _title(page: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    return " ".join(match.group(1).split())[:80] if match else ""


def read(job: Job, get_page: PageGetter) -> Job | None:
    """The job with its full description (and, for Adzuna, the employer's own link), or
    None when the pages gave nothing better."""
    api_text = from_api(job, get_page)
    if api_text and len(api_text) > len(job.description or ""):
        host = (urlsplit(job.url).hostname or "").lower()
        text = keep_old(job.description, api_text)
        log.info("full text read for %s: %s job details, %d characters", job.company, host,
                 len(text))
        return _with_text(job, text, f"job details, {host}", job.url)
    opened = _open(job, get_page)
    if opened is None:
        return None
    final_url, page = opened
    moved = (_is_adzuna(job.url) and not _is_adzuna(final_url)) or (
        job.source == "gmail" and final_url != job.url)  # the alert's tracking link
    text = extract(page)
    host = (urlsplit(final_url).hostname or "").lower()
    if not text or len(text) <= len(job.description or ""):
        # Before 7 Oct this was silent: Pracuj.pl pages opened but gave nothing, and the log
        # had no line at all. The page title shows a block or consent page at once.
        log.info("page read but no job description found for %s: %s, %d characters, "
                 "title %r", job.company, host, len(page), _title(page))
        return replace(job, url=final_url) if moved else None
    log.info("full text read for %s: %s, %d characters", job.company, host, len(text))
    if job.source == "ats":  # a company site's own summary is kept (Adzuna's snippet is cut)
        text = keep_old(job.description, text)
    return _with_text(job, text, f"full page, {host}", final_url if moved else job.url)


def _with_text(job: Job, text: str, origin: str, url: str) -> Job:
    # The full text states every requirement; the snippet may show only the smaller one.
    years = years_required(text)
    posted = experience(text)
    if years is None:
        years, posted = job.years_required, job.experience
    return replace(job, description=text, description_is_snippet=False,
                   description_origin=origin, years_required=years, experience=posted,
                   url=url)


@dataclass
class Outcome:
    tried: int = 0
    read: int = 0
    opened: set[int] = field(default_factory=set)  # id() of the groups whose page was tried


def fill(
    groups: list[list[Job]],
    cfg: Config,
    get_page: PageGetter,
    progress: Progress | None = None,
) -> Outcome:
    """Replace the first job of each group with its full-page version where possible.
    Changes `groups` in place; at most cfg.max_pages pages are read."""
    outcome = Outcome()
    todo = [group for group in groups if needs_text(group[0], cfg)][:cfg.max_pages]

    def done(finished: int, total: int) -> None:
        if progress is not None and (finished == 1 or finished % 5 == 0):
            progress(f"Reading full job descriptions: {finished} of {total}")

    # Several pages at once (cfg.workers); the results are applied here, in list order.
    fulls = run_all(lambda group: read(group[0], get_page), todo, cfg.workers, done)
    for group, full in zip(todo, fulls, strict=True):
        outcome.tried += 1
        outcome.opened.add(id(group))
        if full is not None:
            if full.description != group[0].description:
                outcome.read += 1
            group[0] = full
    return outcome
