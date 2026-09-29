"""LinkedIn descriptions without pasting (no LinkedIn request, no AI).

A LinkedIn email alert has no job description, and LinkedIn is never read, so such a job
waits for /jd. The same job is often also posted on the company's own board (ATS), Adzuna or
Jooble, which the sweep reads anyway. After each sweep, every Unscreened LinkedIn row without
a description is looked up among this sweep's other postings: same company, same title and
same country (the city may be written differently, and "Remote" or "Hybrid" in the title is
ignored). When one matches and has a full description (read from its page when the source
gave only a snippet), that description is written to the LinkedIn row, with a first line
naming where it came from. Rows without a match keep waiting for /jd.

Only an exact company and title match counts: a "Senior DevOps Engineer" posting is never
used for a "DevOps Engineer" alert.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from jobengine.notion_repo import JobsRepo, select_filter
from jobengine.sweep import fulltext
from jobengine.sweep.dedupe import description_blocks
from jobengine.sweep.models import Job
from jobengine.sweep.normalize import canon, canon_city, canon_company, canon_title, without_region

log = logging.getLogger("jobengine.sweep")

LINKEDIN = "LinkedIn"
# Words that only say where or how the job is done, not what it is.
NOISE = {"remote", "hybrid", "onsite", "on-site", "fulltime", "full-time"}
NOISE_PAIRS = (("on", "site"), ("full", "time"))
DEFAULT_MAX_PAGES = 10


def title_key(title: str | None, city: str | None = None, country: str | None = None) -> str:
    """The canonical title without work-mode words and the job's own city or country."""
    words = canon_title(title).split()
    place = set(canon_city(city).split()) | set(canon(city).split()) | set(canon(country).split())
    out: list[str] = []
    i = 0
    while i < len(words):
        if i + 1 < len(words) and (words[i], words[i + 1]) in NOISE_PAIRS:
            i += 2
            continue
        if words[i] not in NOISE and words[i] not in place:
            out.append(words[i])
        i += 1
    return " ".join(out)


def match_key(company: str | None, title: str | None, country: str | None,
              city: str | None = None) -> str:
    return (f"{canon_company(without_region(company or ''))}|{title_key(title, city, country)}"
            f"|{canon(country)}")


def is_linkedin(job: Job) -> bool:
    return job.board.casefold() == LINKEDIN.casefold()


class Pool:
    """This sweep's non-LinkedIn postings that carry a description, by match key."""

    def __init__(self, jobs: list[Job]):
        self.by_key: dict[str, list[Job]] = {}
        for job in jobs:
            if is_linkedin(job) or not (job.description or "").strip():
                continue
            key = match_key(job.company, job.role, job.country, job.city)
            if key.split("|")[1]:
                self.by_key.setdefault(key, []).append(job)

    def __bool__(self) -> bool:
        return bool(self.by_key)

    def find(self, company: str | None, role: str | None, country: str | None,
             city: str | None = None) -> list[Job]:
        """Matches, full descriptions first, then the longest."""
        found = self.by_key.get(match_key(company, role, country, city), [])
        return sorted(found, key=lambda j: (not j.description_is_snippet,
                                            len(j.description or "")), reverse=True)


@dataclass
class Filled:
    labels: list[str] = field(default_factory=list)  # "Company, Role (from Adzuna)"
    snippets_only: int = 0  # matched, but only a short text was found


def _full(job: Job, cfg: fulltext.Config, page: fulltext.PageGetter | None,
          budget: list[int]) -> Job | None:
    """The job with a full description, reading its page once when needed and allowed."""
    if not job.description_is_snippet and len(job.description or "") >= cfg.full_min:
        return job
    if page is None or budget[0] <= 0 or not fulltext.needs_text(job, cfg):
        return None
    group = [job]
    budget[0] -= 1
    fulltext.fill([group], replace(cfg, max_pages=1), page)
    full = group[0]
    if full.description_is_snippet or len(full.description or "") < cfg.full_min:
        return None
    return full


def fill_linkedin(
    repo: JobsRepo,
    jobs: list[Job],
    cfg: fulltext.Config,
    page: fulltext.PageGetter | None,
    with_body: set[str],
    max_pages: int = DEFAULT_MAX_PAGES,
    progress: Callable[[str], None] | None = None,
) -> Filled:
    """Write a matching posting's description to each LinkedIn row that has none."""
    filled = Filled()
    pool = Pool(jobs)
    if not pool:
        return filled
    rows = repo.query_rows({"and": [select_filter("Screen verdict", "Unscreened"),
                                    select_filter("Board", LINKEDIN)]})
    budget = [max(0, max_pages)]
    for page_id, values in rows:
        matches = pool.find(values.get("Company"), values.get("Role"), values.get("Country"),
                            values.get("City"))
        if not matches or page_id in with_body or repo.has_body(page_id):
            continue
        if progress is not None:
            progress(f"LinkedIn descriptions: checking {values.get('Company')}")
        full = next((f for f in (_full(m, cfg, page, budget) for m in matches) if f), None)
        if full is None:
            filled.snippets_only += 1
            continue
        origin = f"same job on {full.board}, found for this LinkedIn alert: {full.url}"
        repo.append_body(page_id, description_blocks(replace(full, description_origin=origin)))
        with_body.add(page_id)
        filled.labels.append(f"{values.get('Company')}, {values.get('Role')} "
                             f"(from {full.board})")
        log.info("LinkedIn row %s: description from %s", page_id, full.url)
    return filled
