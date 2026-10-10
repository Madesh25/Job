"""Alert jobs' descriptions without pasting (no request to the alert's site, no AI).

A LinkedIn email alert has no job description, and LinkedIn is never read, so such a job
waits for /jd. The same holds for the boards in sweep.fulltext.blocked_hosts (Pracuj.pl,
IrishJobs.ie and Jobs.ie answer the bot with HTTP 403; JustJoin IT and theprotocol.it forbid
automatic downloading in their terms, 10 Oct): their alert jobs are saved, their pages are
never opened. The same job is often also posted on the company's own board (ATS), Adzuna or
Jooble, which the sweep reads anyway. After each sweep, every Unscreened row of those boards
without a description is looked up among this sweep's other postings: same company, same title and
same country (the city may be written differently, and "Remote" or "Hybrid" in the title is
ignored). When one matches and has a full description (read from its page when the source
gave only a snippet), that description is written to the LinkedIn row, with a first line
naming where it came from. Rows without a match keep waiting for /jd.

Adzuna rows too (10 Oct): Adzuna gives a 2 to 3 line snippet and its job pages answer HTTP 403
outside the job's country, so screening sees only the snippet and says Needs review. A snippet
row gets the full description of the same job found on another board; it is written after the
snippet, and screening reads the newest description.

Polish boards share many jobs (JustJoin IT, theprotocol.it and Pracuj.pl postings are often
also on NoFluffJobs or the company's own board, which the sweep reads).

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
SNIPPET_BOARDS = ("Adzuna",)  # their rows usually hold only a snippet
DESCRIPTION_HEADER = "Description source:"  # screen/runner.py, the same marker
SNIPPET_MARK = "(snippet only)"
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


def waiting_boards(sender_boards: dict[str, str], blocked: tuple[str, ...]) -> tuple[str, ...]:
    """LinkedIn plus the Board names of the blocked hosts (sweep.gmail.sender_boards)."""
    names = [LINKEDIN]
    for host in blocked:
        name = next((board for domain, board in sender_boards.items()
                     if host == domain or host.endswith("." + domain)
                     or domain.endswith("." + host)), None)
        if name and name not in names:
            names.append(name)
    return tuple(names)


class Pool:
    """This sweep's postings from other boards that carry a description, by match key."""

    def __init__(self, jobs: list[Job], skip: tuple[str, ...] = (LINKEDIN,)):
        self.by_key: dict[str, list[Job]] = {}
        skipped = {b.casefold() for b in skip}
        for job in jobs:
            if job.board.casefold() in skipped or not (job.description or "").strip():
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


def _has_full(blocks: list[str], full_min: int) -> bool:
    """True when the newest description in the page body is a full one (not a snippet), as
    screening reads it (screen/runner.description)."""
    start = max((i for i, b in enumerate(blocks) if b.startswith(DESCRIPTION_HEADER)),
                default=None)
    if start is None:
        # A body without a source line (an old /jd paste) counts as full when it is long.
        return len("".join(blocks).strip()) >= full_min
    body = []
    for text in blocks[start + 1:]:
        if text.startswith(DESCRIPTION_HEADER) or text.startswith("Screening ("):
            break
        body.append(text)
    return SNIPPET_MARK not in blocks[start] and len("".join(body).strip()) >= full_min


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
    boards: tuple[str, ...] = (LINKEDIN,),
) -> Filled:
    """Write a matching posting's description to each waiting row (LinkedIn and the boards
    the bot never reads) that has none."""
    filled = Filled()
    pool = Pool(jobs, skip=boards)
    if not pool:
        return filled
    board_filter = (select_filter("Board", boards[0]) if len(boards) == 1 else
                    {"or": [select_filter("Board", b) for b in boards]})
    rows = repo.query_rows({"and": [select_filter("Screen verdict", "Unscreened"),
                                    board_filter]})
    budget = [max(0, max_pages)]
    for page_id, values in rows:
        matches = pool.find(values.get("Company"), values.get("Role"), values.get("Country"),
                            values.get("City"))
        if not matches or _has_full(repo.read_body(page_id), cfg.full_min):
            continue
        if progress is not None:
            progress(f"Descriptions from other sites: checking {values.get('Company')}")
        full = next((f for f in (_full(m, cfg, page, budget) for m in matches) if f), None)
        if full is None:
            filled.snippets_only += 1
            continue
        origin = (f"same job on {full.board}, found for this {values.get('Board') or LINKEDIN} "
                  f"alert: {full.url}")
        repo.append_body(page_id, description_blocks(replace(full, description_origin=origin)))
        with_body.add(page_id)
        filled.labels.append(f"{values.get('Company')}, {values.get('Role')} "
                             f"(from {full.board})")
        log.info("%s row %s: description from %s", values.get("Board"), page_id, full.url)
    return filled
