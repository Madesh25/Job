"""Data carried through a sweep: raw postings, normalised jobs and the run summary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

COUNTRY_FLAGS = {
    "Poland": "\U0001F1F5\U0001F1F1",
    "Netherlands": "\U0001F1F3\U0001F1F1",
    "Ireland": "\U0001F1EE\U0001F1EA",
}


@dataclass(frozen=True)
class RawPosting:
    """One posting as a source reports it, before any normalising."""

    source: str  # gmail, adzuna, jooble, nofluffjobs, iamexpat or ats
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
    # Where a full description came from when it is not the source's own feed,
    # for example "full page, careers.example.com" (sweep/fulltext.py).
    description_origin: str = ""
    experience: str | None = None  # as posted: "2-3 years", "5+ years"


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
    # ATS only: careers pages that refused us ("Name (HTTP 403)"), and pages with no known board.
    blocked: list[str] = field(default_factory=list)
    no_board: list[str] = field(default_factory=list)
    own_site: list[str] = field(default_factory=list)  # ATS only: sweep.ats.own_site_companies
    notes: list[str] = field(default_factory=list)
    emails: int | None = None  # gmail only: alert emails read


# Where each posting of a sweep ended (FETCH 10). Every posting lands in exactly one bucket,
# so a source's buckets add up to what it read. The first three are kept, the rest dropped.
SAVED = "saved as a new job"
ALREADY = "already in Notion (updated)"
MERGED = "same job from another posting (merged)"
KEPT_BUCKETS = (SAVED, ALREADY, MERGED)
# Dropped jobs remembered for /fetchreport, per source and reason, so every reason keeps
# examples (one cap of 400 for all let the first reasons fill it, 1 Oct test T3).
MAX_DROPPED_PER_REASON = 25


def _fair_pick(jobs: list[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    """Up to `limit` jobs taken in turn from each source, each job once: before 5 Oct the
    first sources filled the list and Jooble's jobs were never shown (test P3)."""
    by_source: dict[str, list[tuple[str, str]]] = {}
    seen: set[tuple[str, str]] = set()
    for item in jobs:
        if item not in seen:
            seen.add(item)
            by_source.setdefault(item[0], []).append(item)
    picked: list[tuple[str, str]] = []
    queues = list(by_source.values())
    while len(picked) < limit and any(queues):
        for queue in queues:
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    return picked


@dataclass
class LossReport:
    """Per source: bucket -> postings. `dropped`: (source, reason, job) for /fetchreport."""

    by_source: dict[str, dict[str, int]] = field(default_factory=dict)
    dropped: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, source: str, bucket: str, job: str = "") -> None:
        counts = self.by_source.setdefault(source, {})
        counts[bucket] = counts.get(bucket, 0) + 1
        if bucket not in KEPT_BUCKETS and job and counts[bucket] <= MAX_DROPPED_PER_REASON:
            self.dropped.append((source, bucket, job))

    def reasons(self) -> list[tuple[str, int]]:
        """Drop reasons with their complete counts, the biggest first."""
        totals: dict[str, int] = {}
        for counts in self.by_source.values():
            for reason, n in counts.items():
                if reason not in KEPT_BUCKETS:
                    totals[reason] = totals.get(reason, 0) + n
        return sorted(totals.items(), key=lambda item: -item[1])

    def read(self, source: str) -> int:
        return sum(self.by_source.get(source, {}).values())

    def lines(self, labels: dict[str, str]) -> list[str]:
        out = []
        for source, counts in self.by_source.items():
            parts = [f"{counts[b]} {b}" for b in KEPT_BUCKETS if counts.get(b)]
            parts += [f"{n} {b}" for b, n in sorted(counts.items(), key=lambda i: -i[1])
                      if b not in KEPT_BUCKETS]
            lines_label = labels.get(source, source)
            out.append(f"- {lines_label}: {self.read(source)} read: " + ", ".join(parts))
        return out

    @classmethod
    def from_state(cls, value: dict[str, Any]) -> LossReport:
        return cls(by_source={k: dict(v) for k, v in (value.get("by_source") or {}).items()},
                   dropped=[tuple(item) for item in value.get("dropped") or []])

    def report_text(self, labels: dict[str, str], when: str, word: str = "",
                    per_reason: int = 5, max_filtered: int = 40) -> str:
        """/fetchreport: the per-source lines and the dropped jobs by reason. `word` keeps
        only reasons or sources that contain it, and then lists more of their jobs."""
        lines = [f"Last /fetch ({when}), where the postings went:", *self.lines(labels)]
        word = word.strip().lower()
        groups: dict[str, list[tuple[str, str]]] = {}
        for source, reason, job in self.dropped:
            name = labels.get(source, source)
            if word and word not in reason.lower() and word not in name.lower():
                continue
            groups.setdefault(reason, []).append((name, job))
        if not groups:
            lines += ["", f"No dropped jobs match {word!r}." if word else "No jobs were dropped."]
            return "\n".join(lines)
        limit = max_filtered if word else per_reason
        lines += ["", "Dropped jobs by reason" + (f" (matching {word!r})" if word else "")
                  + ". /fetchreport <word> shows more of one reason, for example "
                    "/fetchreport skill or /fetchreport adzuna:"]
        totals: dict[str, int] = {}  # the complete counts (only some jobs are kept)
        for source, counts in self.by_source.items():
            name = labels.get(source, source).lower()
            for reason, n in counts.items():
                if reason in groups and (word in reason.lower() or word in name):
                    totals[reason] = totals.get(reason, 0) + n
        for reason, jobs in groups.items():
            totals[reason] = max(totals.get(reason, 0), len(jobs))
        for reason, jobs in sorted(groups.items(), key=lambda item: -totals[item[0]]):
            shown = _fair_pick(jobs, limit)
            lines.append(f"{reason} ({totals[reason]}):")
            lines.extend(f"- {job} [{name}]" for name, job in shown)
            if totals[reason] > len(shown):
                lines.append(f"- ... and {totals[reason] - len(shown)} more")
        if any(n > MAX_DROPPED_PER_REASON for counts in self.by_source.values()
               for reason, n in counts.items() if reason not in KEPT_BUCKETS):
            lines.append(f"(The report keeps up to {MAX_DROPPED_PER_REASON} jobs per source and "
                         "reason; the counts are complete.)")
        return "\n".join(lines)

    def to_state(self, today: str) -> dict[str, Any]:
        return {"at": today, "by_source": self.by_source,
                "dropped": [list(item) for item in self.dropped]}


@dataclass
class SweepSummary:
    new: int = 0
    updated: int = 0
    reposts: int = 0
    skipped: int = 0
    skipped_by: dict[str, int] = field(default_factory=dict)  # reason -> count
    not_written: list[str] = field(default_factory=list)  # jobs Notion refused
    loss: LossReport = field(default_factory=LossReport)  # every posting, per source
    labels: dict[str, str] = field(default_factory=dict)  # source -> name for people
    high_ghost: int = 0
    sources: dict[str, int] = field(default_factory=lambda: {"gmail": 0, "adzuna": 0, "ats": 0})
    not_supported: int = 0
    notes: list[str] = field(default_factory=list)
    high_ghost_jobs: list[str] = field(default_factory=list)
    blocked: str | None = None
    # For the friendly Telegram summary.
    countries: list[str] = field(default_factory=list)  # Config countries.active, in order
    new_by_country: dict[str, int] = field(default_factory=dict)
    not_checked: list[str] = field(default_factory=list)  # plain-language source problems
    not_kept: int = 0  # new jobs below today's best-match cut (sweep.daily_new_limit)
    other_cities: int = 0  # new postings of a job already saved, in another city
    too_senior: int = 0  # asked for more years than screening.max_years_required
    too_old: int = 0  # new postings older than sweep.max_posted_age_days
    max_age_days: int | None = None
    ats_age_days: int | None = None  # company career sites (sweep.ats.max_posted_age_days)
    needs_language: int = 0  # needs a language other than English (sweep/fit.py)
    b2b_only: int = 0  # Poland, B2B contract only (sweep/fit.py)
    no_sponsorship: int = 0  # no visa sponsorship or relocation stated (sweep/fit.py)
    low_match: int = 0  # below sweep.min_skill_match of your tools (sweep/runner.py)
    full_read: int = 0  # job pages that gave a full description (sweep/fulltext.py)
    blocked_sites: list[str] = field(default_factory=list)  # "Name (HTTP 403)"
    no_board_sites: list[str] = field(default_factory=list)
    own_site_count: int = 0  # companies with their own job site, covered by alerts
    full_tried: int = 0
    linkedin_filled: list[str] = field(default_factory=list)  # sweep/crossmatch.py
    backfilled: list[str] = field(default_factory=list)  # sweep/backfill.py
    alert_emails: int | None = None  # email alerts read (None: the source did not run)

    def _skipped_detail(self) -> str:
        if not self.skipped_by:
            return ""
        parts = sorted(self.skipped_by.items(), key=lambda item: -item[1])
        return " (" + ", ".join(f"{SKIP_WORDS.get(why, why)}: {n}" for why, n in parts) + ")"

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

    def friendly_text(self) -> str:
        """Short plain-language summary for Telegram."""
        if self.blocked:
            return self.blocked
        lines = [
            "\u2705 Job search finished",
            "",
            f"\U0001F195 New jobs added to Notion: {self.new}",
        ]
        for country in self.countries or sorted(self.new_by_country):
            flag = COUNTRY_FLAGS.get(country, "")
            lines.append(f"{flag} {country}: {self.new_by_country.get(country, 0)}".strip())
        if self.alert_emails is not None:
            lines.append(f"\U0001F4E7 Email alerts: {self.alert_emails} email(s) read, "
                         f"{self.sources.get('gmail', 0)} job(s) found in them (/alertcheck "
                         "shows each email)")
        lines += [
            "",
            f"\U0001F4C9 Weaker matches not saved (daily limit): {self.not_kept}",
        ]
        if self.too_senior:
            lines.append(f"\U0001F6B7 Asked for more experience than you have (not saved): "
                         f"{self.too_senior}")
        if self.too_old:
            # max_age_days 2 keeps today and the 2 days before: "the last 3 days".
            days = (self.max_age_days or 0) + 1
            when = "before today" if days == 1 else f"before the last {days} days"
            if self.ats_age_days:
                when += f" (company sites: {self.ats_age_days} days)"
            lines.append(f"\U0001F5D3 Posted {when} (not saved): {self.too_old}")
        if self.needs_language:
            lines.append(f"\U0001F5E3 Needs a language other than English (not saved): "
                         f"{self.needs_language}")
        if self.b2b_only:
            lines.append(f"\U0001F4DD B2B contract only (not saved): {self.b2b_only}")
        if self.no_sponsorship:
            lines.append(f"\U0001F6C2 No visa sponsorship or relocation (not saved): "
                         f"{self.no_sponsorship}")
        if self.low_match:
            lines.append(f"\U0001F9E9 Too few of your skills (not saved): {self.low_match}")
        if self.other_cities:
            lines.append(f"\U0001F4CD Same job in another city (not saved again): "
                         f"{self.other_cities}")
        if self.full_tried:
            lines.append(f"\U0001F4C4 Full descriptions read from the job page: "
                         f"{self.full_read} of {self.full_tried}")
        if self.linkedin_filled:
            lines.append(f"\U0001F517 LinkedIn jobs: description found on another site "
                         f"(no /jd needed): {len(self.linkedin_filled)}")
            lines.extend(f"- {job}" for job in self.linkedin_filled)
        if self.backfilled:
            lines.append(f"\U0001F4DD Saved jobs that waited for a description, now filled "
                         f"(/screen can screen them): {len(self.backfilled)}")
            lines.extend(f"- {job}" for job in self.backfilled[:10])
        lines += [
            f"\U0001F501 Already in Notion, seen again: {self.updated + self.reposts}",
            f"\U0001F6AB Not a match (skipped): {self.skipped}{self._skipped_detail()}",
        ]
        if self.loss.by_source:
            lines += ["", "\U0001F4CA Where the postings went (per source; /fetchreport "
                          "lists the dropped jobs):"]
            lines.extend(self.loss.lines(self.labels))
        if self.high_ghost_jobs:
            lines += ["", f"\u26A0\uFE0F Possible ghost jobs (listed for a long time): "
                          f"{self.high_ghost}"]
            lines.extend(f"- {job}" for job in self.high_ghost_jobs)
        if self.blocked_sites or self.no_board_sites:
            lines += ["", "\U0001F3E2 Company sites we could not read:"]
            if self.blocked_sites:
                lines.append(f"- Blocked us: {_names(self.blocked_sites)}")
            if self.no_board_sites:
                lines.append(f"- No job board found: {_names(self.no_board_sites)}")
            lines.append("Put their job board link (for example the myworkdayjobs.com or "
                         "greenhouse.io page) in Careers URL in Target Companies. /sources "
                         "lists them all.")
        if self.own_site_count:
            lines += ["", f"\U0001F3E0 Own job sites, covered by your alerts (not read here): "
                          f"{self.own_site_count} companies"]
        if self.not_written:
            lines += ["", f"\u26A0\uFE0F Could not be saved to Notion (the rest were saved; "
                          f"details in the log): {len(self.not_written)}"]
            lines.extend(f"- {job}" for job in self.not_written[:10])
        if self.not_checked:
            lines += ["", "\u2139\uFE0F Not checked this time:"]
            lines.extend(f"- {item}" for item in self.not_checked)
        return "\n".join(lines)


SKIP_WORDS = {"title not in scope": "title not DevOps-type", "title excluded": "senior or "
              "other excluded title", "location not recognised": "place not recognised",
              "country not active": "other country"}


def _names(items: list[str], limit: int = 8) -> str:
    shown = ", ".join(items[:limit])
    return f"{shown} and {len(items) - limit} more" if len(items) > limit else shown


@dataclass(frozen=True)
class TargetCompany:
    """A Target Companies row (read only)."""

    name: str
    careers_url: str
    ats_platform: str  # Greenhouse, Lever, SmartRecruiters, Workday, Custom or Unknown
    active: bool
    region: str | None = None
