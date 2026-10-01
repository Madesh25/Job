"""Dedupe and write plans (spec section 5). Pure functions, no I/O.

A plan is a dict of Job Opportunities property name -> plain Python value (str, int, date).
The repos turn plans into Notion API payloads. Status and Screen verdict are only ever set
on create; update plans never contain them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from jobengine.sweep.ghost import ghost_risk
from jobengine.sweep.models import Job
from jobengine.sweep.normalize import years_text

MAX_POSTING_IDS = 50
DESCRIPTION_BLOCK_CHARS = 2000
DESCRIPTION_MAX_BLOCKS = 90

# Properties an update plan never contains (First seen is only filled when empty).
NEVER_UPDATED = frozenset({"Status", "Screen verdict", "Dedupe key", "Company", "Role"})


@dataclass
class IndexRow:
    """The parts of an existing Job Opportunities row the sweep needs."""

    page_id: str
    dedupe_key: str
    posting_ids: list[str] = field(default_factory=list)
    times_seen: int = 0
    first_seen: date | None = None
    posted_date: date | None = None
    url: str | None = None
    salary: str | None = None
    years_required: int | None = None
    company: str = ""
    role: str = ""
    city: str | None = None
    visa_flags: list[str] = field(default_factory=list)


def parse_posting_ids(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def format_posting_ids(ids: list[str]) -> str:
    return ", ".join(ids[-MAX_POSTING_IDS:])


def create_plan(job: Job, today: date) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "Company": job.company,
        "Role": job.role,
        "City": job.city,
        "Country": job.country,
        "Board": job.board,
        "URL": job.url,
        "Posted date": job.posted_date,
        "Salary": job.salary,
        "Seniority": job.seniority,
        "Years required": years_text(job.years_required, job.experience,
                                     full=bool(job.description) and not job.description_is_snippet),
        "Dedupe key": job.dedupe_key,
        "Posting IDs": format_posting_ids([job.posting_ref]),
        "First seen": today,
        "Swept date": today,
        "Times seen": 1,
        "Status": "New",
        "Screen verdict": "Unscreened",
        "Ghost job risk": ghost_risk(1, today, job.posted_date, today),
    }
    return {name: value for name, value in plan.items() if value is not None}


@dataclass
class UpdatePlan:
    props: dict[str, Any]
    repost: bool  # True for a new posting ID from a source the row already had
    row: IndexRow  # the row as it will be after the update
    moved_link: str | None = None  # the URL this update replaced (kept on the page)


# Which link to keep when a job is on several sites (your decision of 30 Sep): the
# employer's own page, then a job board, then an aggregator.
AGGREGATOR_HOSTS = ("adzuna.", "jooble.", "europa.eu")
BOARD_HOSTS = ("justjoin.it", "nofluffjobs.com", "pracuj.pl", "theprotocol.it", "bulldogjob.pl",
               "irishjobs.ie", "jobs.ie", "jobsireland.ie", "iamexpat.nl", "indeed.",
               "nationalevacaturebank.nl", "linkedin.com")


def link_rank(url: str | None) -> int:
    """3 employer page, 2 job board, 1 aggregator, 0 no link."""
    if not url:
        return 0
    host = (urlsplit(url).hostname or "").lower()
    if any(part in host for part in AGGREGATOR_HOSTS):
        return 1
    if any(host == b or host.endswith("." + b) or part_in(host, b) for b in BOARD_HOSTS):
        return 2
    return 3


def part_in(host: str, board: str) -> bool:
    return board.endswith(".") and board in host


def _source(posting_ref: str) -> str:
    return posting_ref.split(":", 1)[0]


def update_plan(row: IndexRow, job: Job, today: date) -> UpdatePlan:
    props: dict[str, Any] = {"Swept date": today}
    ids = list(row.posting_ids)
    times_seen = row.times_seen or 1
    new_ref = job.posting_ref not in ids
    # A new ID from a source the row already has is a repost; a first ID from another
    # source is the same job seen on another board and does not count as a sighting.
    repost = new_ref and _source(job.posting_ref) in {_source(ref) for ref in ids}
    if new_ref:
        ids = (ids + [job.posting_ref])[-MAX_POSTING_IDS:]
        props["Posting IDs"] = format_posting_ids(ids)
    if repost:
        times_seen += 1
        props["Times seen"] = times_seen

    # Fill empty fields, never overwrite filled ones.
    fills = {
        "URL": ("url", job.url),
        "Posted date": ("posted_date", job.posted_date),
        "Salary": ("salary", job.salary),
    }
    after = IndexRow(**vars(row))
    after.posting_ids = ids
    after.times_seen = times_seen
    for prop, (attr, value) in fills.items():
        if value is not None and value != "" and getattr(row, attr) in (None, ""):
            props[prop] = value
            setattr(after, attr, value)
    moved = None
    if row.url and job.url and job.url != row.url and link_rank(job.url) > link_rank(row.url):
        props["URL"], after.url, moved = job.url, job.url, row.url
    if row.years_required is None and job.years_required is not None:
        props["Years required"] = years_text(job.years_required, job.experience)
        after.years_required = job.years_required
    if row.first_seen is None:
        # Only happens for rows created by hand without First seen.
        props["First seen"] = today
        after.first_seen = today

    props["Ghost job risk"] = ghost_risk(
        after.times_seen, after.first_seen or today, after.posted_date, today
    )
    return UpdatePlan(props=props, repost=repost, row=after, moved_link=moved)


def description_blocks(job: Job) -> list[str]:
    """Paragraph texts for the page body: a source line, then chunks of the description."""
    if not job.description:
        return []
    header = f"Description source: {job.source}"
    if job.description_origin:
        header += f" ({job.description_origin})"
    elif job.description_is_snippet:
        header += " (snippet only)"
    chunks = [header]
    text = job.description.strip()
    while text and len(chunks) < DESCRIPTION_MAX_BLOCKS:
        chunks.append(text[:DESCRIPTION_BLOCK_CHARS])
        text = text[DESCRIPTION_BLOCK_CHARS:]
    return chunks
