"""Runs one sweep (spec section 2): gate, load, collect, normalise, dedupe and write."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from jobengine import http
from jobengine.config_store import ConfigStore
from jobengine.notion_repo import JobsRepo, NotionClient, NotionReader, jobs_repo_for
from jobengine.reference import Reference
from jobengine.safety import notion_write_target
from jobengine.settings import Settings
from jobengine.sweep import fakes, fulltext
from jobengine.sweep.dedupe import IndexRow, create_plan, description_blocks, update_plan
from jobengine.sweep.gate import strategy_gate
from jobengine.sweep.models import Job, Skipped, SourceResult, SweepSummary, TargetCompany
from jobengine.sweep.normalize import (
    Rules,
    detect_location,
    normalize,
    parse_active_countries,
    title_scope,
)
from jobengine.sweep.rank import Ranker
from jobengine.sweep.sources import adzuna, ats, gmail_alerts, jooble

log = logging.getLogger("jobengine.sweep")

SOURCES = ("gmail", "adzuna", "jooble", "ats")
Prefilter = Callable[[str, str], bool]
Progress = Callable[[str], None]

# Plain names for the Telegram messages.
SOURCE_LABELS = {"gmail": "Email alerts", "adzuna": "Adzuna", "jooble": "Jooble",
                 "ats": "Company career sites"}
PROGRESS_EVERY = 25
# bot_state key read by /sources and /health (Module 07).
LAST_SUMMARY = "sweep.last_summary"
# bot_state key for the daily new-job limit: {"date": "2026-09-27", "count": 12}.
DAY_KEY = "sweep.day"
DEFAULT_DAILY_NEW_LIMIT = 30


class SweepError(Exception):
    """The sweep cannot run at all (for example NOTION_TOKEN is missing)."""


@dataclass
class SweepDeps:
    """Everything a sweep reads from or writes to. Real and fake versions below."""

    config: Callable[[], ConfigStore]
    companies: Callable[[], list[TargetCompany]]
    repo: JobsRepo | None  # None means no write target: DRY RUN, nothing is written
    gmail: Callable[[], SourceResult]
    adzuna: Callable[[], SourceResult]
    # (companies, keep, bot_state) -> postings; bot_state caches the detected boards.
    ats: Callable[..., SourceResult]
    # Skills Inventory, Term Map and Target Companies for the free match score.
    reference: Callable[[], Reference] | None = None
    # Reads one job page for its full description (url -> (final url, html)).
    page: fulltext.PageGetter | None = None
    jooble: Callable[[], SourceResult] = lambda: SourceResult(name="jooble")


def fake_deps(s: Settings, repo: JobsRepo | None = None) -> SweepDeps:
    """Fixtures for every source and an in-memory Job Opportunities. No network.
    `repo` lets the fake bot share one in-memory Job Opportunities with screening."""
    get = fakes.FixtureHttp(s)
    if repo is None and notion_write_target("job_opportunities", s):
        repo = fakes.jobs_repo()
    if repo is None:
        log.warning("DRY RUN: would write to job_opportunities")
    return SweepDeps(
        config=fakes.config,
        companies=fakes.target_companies,
        repo=repo,
        gmail=lambda: gmail_alerts.fetch(s, load_messages=fakes.gmail_messages),
        adzuna=lambda: adzuna.fetch(s, get=get),
        ats=lambda companies, keep, state=None: ats.fetch(
            s, companies, keep, get=get, post=get.post, page=get.page, state=state,
            today=fakes.FAKE_TODAY),
        reference=Reference.fake,
        page=get.page,
        # Jooble has its own tests; the fake sweep keeps its counts without it.
        jooble=lambda: SourceResult(name="jooble"),
    )


def real_deps(s: Settings) -> SweepDeps:
    """Notion, Gmail, Adzuna and ATS feeds for real. Needs NOTION_TOKEN."""
    if not s.notion_token:
        raise SweepError("sweep cannot run: NOTION_TOKEN missing")
    client = NotionClient(s.notion_token, s)
    reader = NotionReader(client, s)
    return SweepDeps(
        config=lambda: ConfigStore.load(client, s),
        companies=reader.target_companies,
        repo=jobs_repo_for(s, client),
        gmail=lambda: gmail_alerts.fetch(s),
        adzuna=lambda: adzuna.fetch(s),
        ats=lambda companies, keep, state=None: ats.fetch(s, companies, keep, state=state),
        reference=lambda: Reference.load(client, s),
        page=lambda url: http.get_page(url, s=s),
        jooble=lambda: jooble.fetch(s),
    )


def _may_lack_body(row: IndexRow) -> bool:
    return all(ref.startswith(gmail_alerts.SOURCE + ":") for ref in row.posting_ids)


def _label(job: Job) -> str:
    return f"{job.company}, {job.role} ({job.city or job.country})"


def _used_today(state: Any, today: date) -> int:
    value = state.get(DAY_KEY) if state is not None else None
    if not value or value.get("date") != today.isoformat():
        return 0
    return int(value.get("count") or 0)


def _ranker(deps: SweepDeps) -> Ranker | None:
    if deps.reference is None:
        return None
    try:
        return Ranker(deps.reference())
    except http.HttpError as exc:
        log.warning("match ranking unavailable (%s): newest jobs first", exc)
        return None


def _sort(groups: list[list[Job]], ranker: Ranker | None, today: date) -> list[list[Job]]:
    def key(jobs: list[Job]) -> tuple[int, date]:
        job = jobs[0]
        return (ranker.score(job, today) if ranker else 0, job.posted_date or date.min)

    return sorted(groups, key=key, reverse=True)


def _rank(deps: SweepDeps, groups: list[list[Job]], today: date) -> list[list[Job]]:
    """New jobs best match first (then newest). Without reference data, newest first."""
    return _sort(groups, _ranker(deps) if groups else None, today)


DEFAULT_MAX_YEARS = 4


def max_years(s: Settings) -> int:
    """The most years of experience a job may ask for (screening.max_years_required)."""
    return int(s.screening.get("max_years_required", DEFAULT_MAX_YEARS))


def too_senior(job: Job, limit: int) -> bool:
    """More years than you have, or a Senior title without years at or under the limit."""
    if job.years_required is not None:
        return job.years_required > limit
    return job.seniority in ("Senior", "Lead")


def _columns(s: Settings, props: dict[str, Any]) -> dict[str, Any]:
    """Drop optional columns this Job Opportunities does not have."""
    if not s.sweep.get("experience_column"):
        props.pop("Experience", None)
    return props


def _job_key(dedupe_key: str) -> str:
    """company|title without the city: the same job offered in several cities."""
    return "|".join(dedupe_key.split("|")[:2])


def _one_per_job(
    ranked: list[list[Job]], index: dict[str, IndexRow]
) -> tuple[list[list[Job]], int]:
    """Drop new jobs that are the same company and title as a row already in Notion or a
    better ranked one in this sweep (one posting listed in 8 cities is one job)."""
    seen = {_job_key(key) for key in index}
    out, dropped = [], 0
    for jobs in ranked:
        key = _job_key(jobs[0].dedupe_key)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(jobs)
    return out, dropped


def _pick(
    s: Settings,
    deps: SweepDeps,
    ranked: list[list[Job]],
    room: int,
    ranker: Ranker | None,
    today: date,
    summary: SweepSummary,
    say: Progress,
) -> list[list[Job]]:
    """The new jobs that fit your experience, best first; the first `room` are saved.

    Jobs asking for more than screening.max_years_required years (or Senior titles without
    years in your range) are dropped. The years are often only in the full description, so
    the job pages are read in rounds from the top of the list: after each round the jobs
    that ask for too much drop out and the next ones are read, until `room + lookahead`
    fitting jobs are found or sweep.fulltext.max_pages pages were read. Then the fitting
    jobs are ranked again (a full description often names skills the snippet left out).
    """
    limit = max_years(s)
    fits: list[list[Job]] = []
    pending: list[list[Job]] = []
    for jobs in ranked:  # known from the snippet already: free
        if too_senior(jobs[0], limit):
            summary.too_senior += 1
        else:
            pending.append(jobs)
    cfg = fulltext.Config.from_settings(s)
    if not cfg.enabled or deps.page is None or room <= 0 or not pending:
        return pending
    want = room + int((s.sweep.get("fulltext") or {}).get("lookahead", 15))
    budget, i = cfg.max_pages, 0
    while i < len(pending) and len(fits) < want and budget > 0:
        chunk = pending[i:i + want - len(fits)]
        i += len(chunk)
        outcome = fulltext.fill(chunk, replace(cfg, max_pages=budget), deps.page, progress=say)
        budget -= outcome.tried
        summary.full_tried += outcome.tried
        summary.full_read += outcome.read
        for jobs in chunk:
            if too_senior(jobs[0], limit):
                summary.too_senior += 1
            else:
                fits.append(jobs)
    if summary.full_tried:
        summary.notes.append(
            f"Full descriptions: read {summary.full_read} of {summary.full_tried} job pages "
            f"(the others kept the short text from their source)."
        )
    return _sort(fits, ranker, today) + pending[i:]


def run_sweep(
    s: Settings,
    deps: SweepDeps,
    today: date,
    sources: Iterable[str] = SOURCES,
    progress: Progress | None = None,
    state: Any = None,
) -> SweepSummary:
    """Run one sweep. `progress` receives short plain-language status lines (for Telegram).
    With `state` (bot_state), the counts are stored for /sources."""

    def say(text: str) -> None:
        if progress is not None:
            try:
                progress(text)
            except Exception:  # progress must never break the sweep
                log.exception("progress callback failed")

    sources = [name for name in SOURCES if name in set(sources)]
    config = deps.config()
    blocked = strategy_gate(config, today, state, s)
    if blocked:
        return SweepSummary(blocked=blocked)

    rules = Rules.from_config(s.sweep)
    active = parse_active_countries(config.get("countries.active"))

    def keep(title: str, location: str) -> bool:
        country, _ = detect_location(location, rules)
        return title_scope(title, rules) is None and country in active

    summary = SweepSummary(countries=list(active))
    repo = deps.repo
    if repo is None:
        summary.notes.append("DRY RUN: would write to job_opportunities (no rows written)")
        summary.not_checked.append("Notion: no write target here, nothing was saved")
    say("Checking what is already in your Notion...")
    log.info("loading Job Opportunities index...")
    index: dict[str, IndexRow] = {row.dedupe_key: row for row in repo.load_index()} if repo else {}
    log.info("index: %d existing rows", len(index))
    companies = deps.companies() if "ats" in sources else []

    results: list[SourceResult] = []
    found = 0
    for name in sources:
        say(f"Searching {SOURCE_LABELS[name]}... ({found} jobs found so far)")
        try:
            if name == "ats":
                result = deps.ats(companies, keep, state)
            else:
                result = getattr(deps, name)()
        except http.HttpError as exc:
            result = SourceResult(name=name, skipped_reason=f"{name} failed: {exc}")
        summary.sources[name] = len(result.postings)
        log.info("%s: %d postings collected", name, len(result.postings))
        summary.not_supported += len(result.not_supported)
        summary.blocked_sites.extend(result.blocked)
        summary.no_board_sites.extend(result.no_board)
        found += len(result.postings)
        if result.skipped_reason:
            summary.notes.append(result.skipped_reason)
            problem = "not set up yet" if "missing" in result.skipped_reason else "failed this time"
            summary.not_checked.append(f"{SOURCE_LABELS[name]}: {problem}")
        summary.notes.extend(result.notes)
        results.append(result)

    total = sum(len(result.postings) for result in results)
    log.info("normalising and writing %d postings...", total)
    say(f"Found {total} jobs. Saving to your Notion...")
    done = 0
    touched: dict[str, tuple[str, str]] = {}  # dedupe key -> (label, ghost risk)
    with_body: set[str] = set()
    dry_ids = 0

    def update_existing(row: IndexRow, job: Job) -> None:
        plan_update = update_plan(row, job, today)
        _columns(s, plan_update.props)
        if repo:
            repo.update(row.page_id, plan_update.props)
            if job.description and row.page_id not in with_body:
                # Only rows seen so far through email alerts can lack a description:
                # every other source writes one when it creates the row. Checking just
                # those rows halves the Notion requests on a daily re-sweep.
                if _may_lack_body(row) and not repo.has_body(row.page_id):
                    repo.append_body(row.page_id, description_blocks(job))
                with_body.add(row.page_id)
        index[job.dedupe_key] = plan_update.row
        if plan_update.repost:
            summary.reposts += 1
        else:
            summary.updated += 1
        place = row.city or job.city or job.country
        label = f"{row.company or job.company}, {row.role or job.role} ({place})"
        touched[job.dedupe_key] = (label, plan_update.props["Ghost job risk"])

    def create_new(job: Job) -> None:
        nonlocal dry_ids
        plan = _columns(s, create_plan(job, today))
        blocks = description_blocks(job)
        if repo:
            page_id = repo.create(plan, blocks)
        else:
            dry_ids += 1
            page_id = f"dry-run-{dry_ids}"
        if blocks:
            with_body.add(page_id)
        index[job.dedupe_key] = IndexRow(
            page_id=page_id, dedupe_key=job.dedupe_key, posting_ids=[job.posting_ref],
            times_seen=1, first_seen=today, posted_date=job.posted_date, url=job.url,
            salary=job.salary, years_required=job.years_required,
            company=job.company, role=job.role, city=job.city,
        )
        summary.new += 1
        summary.new_by_country[job.country] = summary.new_by_country.get(job.country, 0) + 1
        touched[job.dedupe_key] = (_label(job), plan["Ghost job risk"])

    # Pass 1: rows already in Notion are updated; new jobs wait to be ranked.
    candidates: dict[str, list[Job]] = {}  # dedupe key -> postings, first one creates
    for result in results:
        for raw in result.postings:
            done += 1
            if done % PROGRESS_EVERY == 0:
                log.info("... %d of %d postings processed", done, total)
                say(f"Saving to your Notion: {done} of {total} jobs checked")
            outcome = normalize(raw, rules, active)
            if isinstance(outcome, Skipped):
                summary.skipped += 1
                log.debug("skipped %s: %s", raw.title, outcome.reason)
                continue
            row = index.get(outcome.dedupe_key)
            if row is None:
                candidates.setdefault(outcome.dedupe_key, []).append(outcome)
            else:
                update_existing(row, outcome)

    # Pass 2: keep the best new jobs up to what is left of today's limit (free, no LLM).
    limit = max(0, int(s.sweep.get("daily_new_limit", DEFAULT_DAILY_NEW_LIMIT)))
    used = _used_today(state, today)
    room = max(0, limit - used)
    ranker = _ranker(deps) if candidates else None
    ranked = _sort(list(candidates.values()), ranker, today)
    ranked, summary.other_cities = _one_per_job(ranked, index)
    ranked = _pick(s, deps, ranked, room, ranker, today, summary, say)
    kept = ranked[:room]
    for jobs in kept:
        create_new(jobs[0])
        for extra in jobs[1:]:  # the same job twice in one sweep: a second posting ID
            update_existing(index[extra.dedupe_key], extra)
    summary.not_kept = len(ranked) - len(kept)
    if summary.not_kept:
        summary.notes.append(
            f"Kept the best {len(kept)} of {len(ranked)} new jobs (daily limit {limit}, "
            f"{used} already added today). {summary.not_kept} weaker matches were not saved."
        )
    if repo and state is not None and kept:
        try:
            state.set(DAY_KEY, {"date": today.isoformat(), "count": used + len(kept)})
        except Exception:  # never fail a sweep over the counter
            log.exception("could not store the daily new-job count")

    high = sorted(label for label, risk in touched.values() if risk == "High")
    summary.high_ghost = len(high)
    summary.high_ghost_jobs = high
    if state is not None:
        try:
            state.set(LAST_SUMMARY, {"at": today.isoformat(), "new": summary.new,
                                     "updated": summary.updated,
                                     "sources": dict(summary.sources),
                                     "blocked_sites": summary.blocked_sites,
                                     "no_board_sites": summary.no_board_sites})
        except Exception:  # never fail a sweep over the /sources counters
            log.exception("could not store the sweep summary")
    return summary
