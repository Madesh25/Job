"""Data carried through a sweep: raw postings, normalised jobs and the run summary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class RawPosting:
    """One posting as a source reports it, before any normalising."""

    source: str  # gmail, adzuna or ats
    board: str  # Board option in Job Opportunities, for example "LinkedIn" or "Company site"
    title: str
    company: str
    location_text: str
    url: str
    posting_id: str
    posted_date: date | None = None
    salary_text: str | None = None
    description: str | None = None
    description_is_snippet: bool = False
    # Structured location parts when the source has them (Adzuna location.area).
    location_area: tuple[str, ...] = ()


@dataclass(frozen=True)
class Job:
    """A posting that passed the scope filters, ready to be written."""

    source: str
    board: str
    company: str
    role: str
    city: str | None
    country: str
    url: str
    posted_date: date | None
    salary: str | None
    seniority: str
    years_required: int | None
    dedupe_key: str
    posting_ref: str  # "{source}:{posting_id}"
    description: str | None
    description_is_snippet: bool


@dataclass(frozen=True)
class Skipped:
    """A posting dropped as out of scope, with the reason."""

    posting: RawPosting
    reason: str


@dataclass
class SourceResult:
    """What one source returned in a run."""

    name: str
    postings: list[RawPosting] = field(default_factory=list)
    skipped_reason: str | None = None  # set when the whole source was skipped
    not_supported: list[str] = field(default_factory=list)  # company names (ATS only)
    notes: list[str] = field(default_factory=list)


@dataclass
class SweepSummary:
    new: int = 0
    updated: int = 0
    reposts: int = 0
    skipped: int = 0
    high_ghost: int = 0
    sources: dict[str, int] = field(default_factory=lambda: {"gmail": 0, "adzuna": 0, "ats": 0})
    not_supported: int = 0
    notes: list[str] = field(default_factory=list)
    high_ghost_jobs: list[str] = field(default_factory=list)
    blocked: str | None = None

    def text(self) -> str:
        if self.blocked:
            return self.blocked
        lines = [
            f"Sweep done: {self.new} new, {self.updated} updated, {self.reposts} reposts, "
            f"{self.skipped} skipped (out of scope), {self.high_ghost} high ghost risk. "
            f"Sources: gmail {self.sources.get('gmail', 0)}, "
            f"adzuna {self.sources.get('adzuna', 0)}, ats {self.sources.get('ats', 0)}. "
            f"Not supported: {self.not_supported} companies."
        ]
        lines.extend(self.notes)
        if self.high_ghost_jobs:
            lines.append("High ghost risk (kept, check before applying):")
            lines.extend(f"- {job}" for job in self.high_ghost_jobs)
        return "\n".join(lines)
