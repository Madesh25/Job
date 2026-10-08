"""Descriptions for saved jobs that still wait for one (8 Oct).

Until PR #97 a job below the sweep's reading rounds was saved without its page being read:
the Pracuj.pl alert jobs of 6 and 7 Oct have no description and /screen leaves them
"waiting for JD". After each sweep, up to sweep.fulltext.backfill_pages Unscreened rows with
an empty page body get their job page read (same rules as fulltext: public https only,
never LinkedIn) and the description written to the page, so the next /screen can screen
them. A page that gives nothing leaves the row as it was.

Phase 6 (8 Oct): Pracuj.pl answers HTTP 403 to the bot, and every /fetch opened the same
blocked pages again. Now a row whose page gave nothing is noted (screen/waiting.py): it is
listed in /jd and not read again for waiting.RETRY_DAYS days; a row on a blocked site
(sweep.fulltext.blocked_hosts) is noted without being opened and never retried.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from jobengine import http
from jobengine.notion_repo import JobsRepo, select_filter
from jobengine.screen import waiting
from jobengine.sweep import fulltext
from jobengine.sweep.dedupe import description_blocks
from jobengine.sweep.models import Job

log = logging.getLogger("jobengine.sweep")

DEFAULT_PAGES = 20
# Rows looked at per sweep (each costs one Notion call to see whether it has a body).
CHECKS_PER_PAGE = 5


def _job(values: dict[str, Any]) -> Job:
    return Job(source="saved", board=values.get("Board") or "", company=values.get("Company")
               or "", role=values.get("Role") or "", city=values.get("City"),
               country=values.get("Country") or "", url=values.get("URL") or "",
               posted_date=None, salary=None, seniority="Unknown", years_required=None,
               dedupe_key="", posting_ref="", description=None, description_is_snippet=True)


def readable(values: dict[str, Any]) -> bool:
    url = (values.get("URL") or "").strip().lower()
    return url.startswith("https://") and "linkedin" not in url and \
        "linkedin" not in (values.get("Board") or "").lower()


def fill_waiting(
    repo: JobsRepo,
    page: fulltext.PageGetter,
    with_body: set[str],
    max_pages: int = DEFAULT_PAGES,
    progress: Callable[[str], None] | None = None,
    state: Any = None,
    today: date | None = None,
    blocked: tuple[str, ...] = (),
) -> list[str]:
    """Write a description to saved Unscreened rows that have none. Returns the labels
    ("Company, Role") of the rows filled. With `state` and `today`, rows whose page gave
    nothing are noted for /jd and left alone for a while."""
    filled: list[str] = []
    if max_pages <= 0:
        return filled
    known = waiting.noted(state)
    recent = waiting.recently_tried(state, today) if today is not None else set()
    failed: list[str] = []
    tried = checked = 0
    for page_id, values in repo.query_rows(select_filter("Screen verdict", "Unscreened")):
        if tried >= max_pages or checked >= max_pages * CHECKS_PER_PAGE:
            break
        if page_id in with_body or not readable(values) or page_id in recent:
            continue
        host = (urlsplit(values.get("URL") or "").hostname or "").lower()
        on_blocked = bool(blocked) and http.host_in(host, blocked)
        if on_blocked and page_id in known:
            continue  # waits for /jd; the site will not answer next time either
        checked += 1
        if repo.has_body(page_id):
            continue
        label = f"{values.get('Company')}, {values.get('Role')}"
        if on_blocked:
            failed.append(page_id)
            continue
        tried += 1
        if progress is not None:
            progress(f"Descriptions for saved jobs: reading {values.get('Company')}")
        try:
            full = fulltext.read(_job(values), page)
        except Exception as exc:  # one page never stops the sweep
            log.info("saved job page not read for %s: %s", label, exc)
            full = None
        if full is None or not full.description or full.description_is_snippet:
            failed.append(page_id)
            continue
        repo.append_body(page_id, description_blocks(full))
        with_body.add(page_id)
        filled.append(label)
        log.info("saved job %s: description added (%d characters)", label,
                 len(full.description))
    if today is not None and state is not None:
        if failed:
            log.info("%d saved jobs still have no description: listed in /jd", len(failed))
            waiting.note(state, failed, today, tried=True)
        waiting.forget(state, (pid for pid in with_body if pid in known))
    return filled
