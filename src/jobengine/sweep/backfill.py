"""Descriptions for saved jobs that still wait for one (8 Oct).

Until PR #97 a job below the sweep's reading rounds was saved without its page being read:
the Pracuj.pl alert jobs of 6 and 7 Oct have no description and /screen leaves them
"waiting for JD". After each sweep, up to sweep.fulltext.backfill_pages Unscreened rows with
an empty page body get their job page read (same rules as fulltext: public https only,
never LinkedIn) and the description written to the page, so the next /screen can screen
them. A page that gives nothing leaves the row as it was.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from jobengine.notion_repo import JobsRepo, select_filter
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
) -> list[str]:
    """Write a description to saved Unscreened rows that have none. Returns the labels
    ("Company, Role") of the rows filled."""
    filled: list[str] = []
    if max_pages <= 0:
        return filled
    tried = checked = 0
    for page_id, values in repo.query_rows(select_filter("Screen verdict", "Unscreened")):
        if tried >= max_pages or checked >= max_pages * CHECKS_PER_PAGE:
            break
        if page_id in with_body or not readable(values):
            continue
        checked += 1
        if repo.has_body(page_id):
            continue
        tried += 1
        label = f"{values.get('Company')}, {values.get('Role')}"
        if progress is not None:
            progress(f"Descriptions for saved jobs: reading {values.get('Company')}")
        try:
            full = fulltext.read(_job(values), page)
        except Exception as exc:  # one page never stops the sweep
            log.info("saved job page not read for %s: %s", label, exc)
            continue
        if full is None or not full.description or full.description_is_snippet:
            continue
        repo.append_body(page_id, description_blocks(full))
        with_body.add(page_id)
        filled.append(label)
        log.info("saved job %s: description added (%d characters)", label,
                 len(full.description))
    return filled
