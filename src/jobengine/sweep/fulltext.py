"""Full job descriptions read from the posting's own page (free, no LLM, no login).

Adzuna gives only a snippet, and SmartRecruiters or Workday postings past their detail
budget come with no description at all. For the new jobs that may be saved today, the sweep
reads the posting page (http.get_page: plain GET, public https hosts only, never linkedin,
size limit) and keeps the whole description:

1. a schema.org JobPosting in the page's JSON-LD (most job boards and ATS pages have one)
2. otherwise the largest block that looks like a job description (main, article, or an
   element whose id or class names a description)
3. otherwise the whole visible page text

A page that cannot be read keeps the snippet. Email alert links are not read by default
(sweep.fulltext.sources): they are tracking links, and LinkedIn is never read at all.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
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
DEFAULT_SOURCES = ("adzuna", "ats", "jooble")
# Least text accepted: a JobPosting is trusted when short, a guessed block less so and
# the whole page text only when it is clearly more than a snippet.
MIN_JSON_LD = 150
MIN_BLOCK = 250
MIN_PAGE = 600
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

    @classmethod
    def from_settings(cls, s: Settings) -> Config:
        cfg = s.sweep.get("fulltext") or {}
        return cls(
            enabled=bool(cfg.get("enabled", True)),
            max_pages=max(0, int(cfg.get("max_pages", DEFAULT_MAX_PAGES))),
            sources=tuple(cfg.get("sources") or DEFAULT_SOURCES),
            full_min=int(s.screening.get("full_min_chars", 600)),
            workers=max(1, int(s.sweep.get("workers", 8))),
        )


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


def _blocks(soup: BeautifulSoup) -> Iterator[Tag]:
    yield from soup.find_all(["main", "article"])
    for tag in soup.find_all(True):
        names = " ".join([tag.get("id") or "", *(tag.get("class") or [])])
        if names.strip() and HINT.search(names):
            yield tag


def extract(page: str) -> str | None:
    """The full job description in a page, or None when there is not enough text."""
    soup = BeautifulSoup(page, "html.parser")
    text = from_json_ld(soup)
    if text and len(text) >= MIN_JSON_LD:
        return text
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


def read(job: Job, get_page: PageGetter) -> Job | None:
    """The job with its full description (and, for Adzuna, the employer's own link), or
    None when the pages gave nothing better."""
    opened = _open(job, get_page)
    if opened is None:
        return None
    final_url, page = opened
    moved = _is_adzuna(job.url) and not _is_adzuna(final_url)
    text = extract(page)
    if not text or len(text) <= len(job.description or ""):
        return replace(job, url=final_url) if moved else None
    host = (urlsplit(final_url).hostname or "").lower()
    years = job.years_required if job.years_required is not None else years_required(text)
    return replace(job, description=text, description_is_snippet=False,
                   description_origin=f"full page, {host}", years_required=years,
                   experience=job.experience or experience(text),
                   url=final_url if moved else job.url)


@dataclass
class Outcome:
    tried: int = 0
    read: int = 0


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
        if full is not None:
            if full.description != group[0].description:
                outcome.read += 1
            group[0] = full
    return outcome
