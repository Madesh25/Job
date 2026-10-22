"""NoFluffJobs (FETCH 4): IT jobs in Poland, read from the public search page.

The search page carries its results as page data (`<script id="serverApp-state">`, the
Angular transfer state): title, company, cities, posted date, salary and the job's address.
Built from the probe samples of 5 Oct (out/probes/nofluffjobs-*.html). One page per search
term (newest first), robots.txt respected, one request a second, plain GET, no login. The
description is read from the job page by sweep/fulltext.py, like every other source.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote_plus
from urllib.robotparser import RobotFileParser

from jobengine import http
from jobengine.settings import Settings
from jobengine.sweep.models import RawPosting, SourceResult

log = logging.getLogger("jobengine.sweep")

SOURCE = "nofluffjobs"
BOARD = "NoFluffJobs"
SITE = "https://nofluffjobs.com"
SEARCH_URL = SITE + "/pl/?criteria=keyword%3D{q}&sort=newest&page={page}"
JOB_URL = SITE + "/pl/job/{slug}"
DEFAULT_TERMS = ("devops engineer", "site reliability engineer", "platform engineer",
                 "cloud engineer", "kubernetes engineer", "infrastructure engineer",
                 "devsecops engineer")
# The page data block. 22 Oct: every search answered with 0 jobs, most likely because the
# site moved to a newer Angular, which names the block "ng-state" (it was "serverApp-state")
# and no longer escapes quotes as &q;. Any JSON script block is read now, and the job list
# is looked for at any depth.
STATE = re.compile(r'<script\b[^>]*type="application/json"[^>]*>(.*?)</script>', re.S | re.I)
SCRIPT_ID = re.compile(r'<script\b[^>]*\bid="([^"]+)"[^>]*type="application/json"', re.I)
# Angular escapes these characters in its older transfer state.
UNESCAPE = (("&q;", '"'), ("&s;", "'"), ("&l;", "<"), ("&g;", ">"), ("&a;", "&"))
PAUSE_SECONDS = 1.0
MAX_DEPTH = 8

Getter = Callable[[str], tuple[str, str]]  # url -> (final url, page text)


def _job_list(value: Any, depth: int = 0) -> list[dict[str, Any]] | None:
    """The first `postings` list of jobs (dicts with a url and a title) inside `value`."""
    if depth > MAX_DEPTH:
        return None
    if isinstance(value, dict):
        found = value.get("postings")
        if isinstance(found, list) and any(
                isinstance(j, dict) and j.get("url") and j.get("title") for j in found):
            return found
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        jobs = _job_list(child, depth + 1)
        if jobs is not None:
            return jobs
    return None


def postings_from_page(text: str) -> list[dict[str, Any]]:
    """The job list in a search page, or [] when the page has none (layout changed)."""
    for raw in STATE.findall(text):
        if "&q;" in raw:
            for old, new in UNESCAPE:
                raw = raw.replace(old, new)
        try:
            state = json.loads(raw)
        except ValueError:
            continue
        jobs = _job_list(state)
        if jobs is not None:
            return jobs
    return []


def page_shape(text: str) -> str:
    """What a search page holds when no job list was found, for the log (no content)."""
    ids = list(dict.fromkeys(SCRIPT_ID.findall(text)))[:5]
    return (f"{len(text)} characters, JSON blocks: {', '.join(ids) or 'none'}, "
            f"word 'postings' {'present' if 'postings' in text else 'absent'}")


def _day(ms: Any) -> date | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _salary(value: Any) -> str | None:
    if not isinstance(value, dict) or value.get("from") is None:
        return None
    low, high = value.get("from"), value.get("to")
    amount = f"{low}" if high in (None, low) else f"{low} - {high}"
    period = str(value.get("period") or "Month").lower()
    kind = f", {value['type']}" if value.get("type") else ""
    return f"{amount} {value.get('currency') or ''} per {period}{kind} (NoFluffJobs)"


def to_posting(job: dict[str, Any]) -> RawPosting | None:
    slug = job.get("url")
    title = " ".join(str(job.get("title") or "").split())
    if not slug or not title:
        return None
    places = [p for p in ((job.get("location") or {}).get("places") or [])
              if isinstance(p, dict)]
    cities = [p.get("city") for p in places if p.get("city") and p.get("city") != "Remote"]
    countries = [((p.get("country") or {}).get("name")) for p in places]
    country = next((c for c in countries if c), "")
    remote = bool(job.get("fullyRemote") or (job.get("location") or {}).get("fullyRemote"))
    first = cities[0] if cities else ("Remote" if remote else "")
    location = ", ".join(part for part in (first, country) if part)
    return RawPosting(
        source=SOURCE,
        board=BOARD,
        title=title,
        company=" ".join(str(job.get("name") or "(unknown)").split()) or "(unknown)",
        location_text=location,
        url=JOB_URL.format(slug=slug),
        posting_id=str(job.get("reference") or job.get("id") or slug),
        posted_date=_day(job.get("posted")),
        salary_text=_salary(job.get("salary")),
        description=None,
        location_area=tuple(dict.fromkeys(c for c in [*cities[1:4], country] if c)),
    )


def _robots(get: Getter) -> RobotFileParser | None:
    try:
        _, text = get(SITE + "/robots.txt")
    except http.HttpError:
        return None
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    return parser


def fetch(s: Settings, get: Getter | None = None,
          pause: Callable[[float], None] = time.sleep) -> SourceResult:
    """NoFluffJobs source. `get` is the fake; the default goes through jobengine.http."""
    cfg = s.sweep.get("nofluffjobs") or {}
    result = SourceResult(name=SOURCE)
    if not cfg.get("enabled", True):
        result.skipped_reason = "nofluffjobs skipped: disabled in sweep.nofluffjobs"
        return result
    if get is None:
        def get(url: str) -> tuple[str, str]:
            return http.get_page(url, s=s)

    robots = _robots(get)
    seen: set[str] = set()
    pages = max(1, int(cfg.get("pages", 1)))
    for term in cfg.get("search_terms") or DEFAULT_TERMS:
        for page in range(1, pages + 1):
            url = SEARCH_URL.format(q=quote_plus(term), page=page)
            if robots is not None and not robots.can_fetch(http.PAGE_USER_AGENT, url):
                result.notes.append("nofluffjobs: robots.txt disallows the search, skipped")
                return result
            pause(PAUSE_SECONDS)
            try:
                _, text = get(url)
            except http.HttpError as exc:
                result.notes.append(f"nofluffjobs {term!r}: failed (HTTP {exc.status or '?'})")
                return result  # blocked or changed: stop quietly, the summary says so
            jobs = postings_from_page(text)
            log.info("nofluffjobs %r page %d: %d postings", term, page, len(jobs))
            if not jobs:
                if page == 1:
                    shape = page_shape(text)
                    log.info("nofluffjobs %r: no job list found (%s)", term, shape)
                    result.notes.append(f"nofluffjobs {term!r}: no jobs found on the page "
                                        f"(the page layout may have changed: {shape})")
                break
            for job in jobs:
                posting = to_posting(job)
                if posting and posting.posting_id not in seen:
                    seen.add(posting.posting_id)
                    result.postings.append(posting)
    return result
