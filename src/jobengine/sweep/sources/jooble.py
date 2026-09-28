"""Jooble job search API: jobs from many job boards, Ireland included (Adzuna has none).

Needs a free key (JOOBLE_API_KEY, https://jooble.org/api/about); without it the source is
skipped. One POST per country and search term, one page each (sweep.jooble). Jooble gives a
snippet; the full description is read from the job page for jobs that may be saved
(sweep/fulltext.py). The key is part of the URL path and is never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from jobengine import http
from jobengine.settings import Settings
from jobengine.sweep.models import RawPosting, SourceResult
from jobengine.sweep.sources.ats import html_to_text

log = logging.getLogger("jobengine.sweep")

SOURCE = "jooble"
BOARD = "Other"
API_URL = "https://jooble.org/api/{key}"
DEFAULT_COUNTRIES = ("Ireland", "Poland", "Netherlands")
DEFAULT_TERMS = ("devops engineer", "site reliability engineer", "platform engineer",
                 "cloud engineer")

Poster = Callable[[str, Any], Any]  # url, JSON body -> JSON answer


def _posted(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def board_for(job: dict[str, Any], boards: dict[str, str]) -> str:
    """The Board of the site Jooble found the job on (sweep.gmail.sender_boards), else Other."""
    source = str(job.get("source") or "").lower().removeprefix("www.")
    matches = [d for d in boards if source == d or source.endswith("." + d)]
    return boards[max(matches, key=len)] if matches else BOARD


def to_posting(job: dict[str, Any], country: str, boards: dict[str, str]) -> RawPosting:
    location = job.get("location") or ""
    if country.lower() not in location.lower():
        location = f"{location}, {country}" if location else country
    snippet = html_to_text(job.get("snippet"))
    return RawPosting(
        source=SOURCE,
        board=board_for(job, boards),
        title=(html_to_text(job.get("title")) or "").replace("\n", " "),
        company=(job.get("company") or "(unknown)").strip() or "(unknown)",
        location_text=location,
        url=job.get("link") or "",
        posting_id=str(job.get("id") or job.get("link") or ""),
        posted_date=_posted(job.get("updated")),
        salary_text=(f"{job['salary']} (Jooble)" if job.get("salary") else None),
        description=snippet,
        description_is_snippet=True,
    )


def fetch(s: Settings, post: Poster | None = None) -> SourceResult:
    """Jooble source. `post` is the fake; the default goes through jobengine.http."""
    cfg = s.sweep.get("jooble") or {}
    result = SourceResult(name=SOURCE)
    if not cfg.get("enabled", True):
        result.skipped_reason = "jooble skipped: disabled in sweep.jooble"
        return result
    if post is None:
        if not s.jooble_api_key:
            result.skipped_reason = "jooble skipped: JOOBLE_API_KEY missing"
            return result

        def post(url: str, body: Any) -> Any:
            return http.post_json(url, json=body, s=s)

    url = API_URL.format(key=s.jooble_api_key or "fake")
    per_page = int(cfg.get("results_per_page", 50))
    boards = dict((s.sweep.get("gmail") or {}).get("sender_boards") or {})
    seen: set[str] = set()
    for country in cfg.get("countries") or DEFAULT_COUNTRIES:
        for term in cfg.get("search_terms") or DEFAULT_TERMS:
            body = {"keywords": term, "location": country, "page": "1",
                    "ResultOnPage": str(per_page)}
            try:
                data = post(url, body) or {}
            except http.HttpError as exc:
                # The error text has the host and path; the path is the key, so hide it.
                result.notes.append(f"jooble {country}: failed (HTTP {exc.status or '?'})")
                return result
            jobs = data.get("jobs") or []
            log.info("jooble %s %r: %d postings", country, term, len(jobs))
            for job in jobs:
                posting = to_posting(job, country, boards)
                if posting.posting_id and posting.posting_id not in seen:
                    seen.add(posting.posting_id)
                    result.postings.append(posting)
    return result
