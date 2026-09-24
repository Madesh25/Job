"""Runs one sweep (spec section 2): gate, load, collect, normalise, dedupe and write."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from jobengine import http
from jobengine.notion_repo import JobsRepo, NotionClient, NotionReader, jobs_repo_for
from jobengine.safety import notion_write_target
from jobengine.settings import Settings
from jobengine.sweep import fakes
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
from jobengine.sweep.sources import adzuna, ats, gmail_alerts

log = logging.getLogger("jobengine.sweep")

SOURCES = ("gmail", "adzuna", "ats")
Prefilter = Callable[[str, str], bool]


class SweepError(Exception):
    """The sweep cannot run at all (for example NOTION_TOKEN is missing)."""


@dataclass
class SweepDeps:
    """Everything a sweep reads from or writes to. Real and fake versions below."""

    config: Callable[[], Mapping[str, str]]
    companies: Callable[[], list[TargetCompany]]
    repo: JobsRepo | None  # None means no write target: DRY RUN, nothing is written
    gmail: Callable[[], SourceResult]
    adzuna: Callable[[], SourceResult]
    ats: Callable[[list[TargetCompany], Prefilter], SourceResult]


def fake_deps(s: Settings) -> SweepDeps:
    """Fixtures for every source and an in-memory Job Opportunities. No network."""
    get = fakes.FixtureHttp(s)
    repo = fakes.jobs_repo() if notion_write_target("job_opportunities", s) else None
    if repo is None:
        log.warning("DRY RUN: would write to job_opportunities")
    return SweepDeps(
        config=fakes.config,
        companies=fakes.target_companies,
        repo=repo,
        gmail=lambda: gmail_alerts.fetch(s, load_messages=fakes.gmail_messages),
        adzuna=lambda: adzuna.fetch(s, get=get),
        ats=lambda companies, keep: ats.fetch(s, companies, keep, get=get),
    )


def real_deps(s: Settings) -> SweepDeps:
    """Notion, Gmail, Adzuna and ATS feeds for real. Needs NOTION_TOKEN."""
    if not s.notion_token:
        raise SweepError("sweep cannot run: NOTION_TOKEN missing")
    client = NotionClient(s.notion_token, s)
    reader = NotionReader(client, s)
    return SweepDeps(
        config=reader.config,
        companies=reader.target_companies,
        repo=jobs_repo_for(s, client),
        gmail=lambda: gmail_alerts.fetch(s),
        adzuna=lambda: adzuna.fetch(s),
        ats=lambda companies, keep: ats.fetch(s, companies, keep),
    )


def _label(job: Job) -> str:
    return f"{job.company}, {job.role} ({job.city or job.country})"


def run_sweep(
    s: Settings,
    deps: SweepDeps,
    today: date,
    sources: Iterable[str] = SOURCES,
) -> SweepSummary:
    sources = [name for name in SOURCES if name in set(sources)]
    config = deps.config()
    blocked = strategy_gate(config, today)
    if blocked:
        return SweepSummary(blocked=blocked)

    rules = Rules.from_config(s.sweep)
    active = parse_active_countries(config.get("countries.active"))

    def keep(title: str, location: str) -> bool:
        country, _ = detect_location(location, rules)
        return title_scope(title, rules) is None and country in active

    summary = SweepSummary()
    repo = deps.repo
    if repo is None:
        summary.notes.append("DRY RUN: would write to job_opportunities (no rows written)")
    index: dict[str, IndexRow] = {row.dedupe_key: row for row in repo.load_index()} if repo else {}
    companies = deps.companies() if "ats" in sources else []

    results: list[SourceResult] = []
    for name in sources:
        try:
            if name == "ats":
                result = deps.ats(companies, keep)
            else:
                result = getattr(deps, name)()
        except http.HttpError as exc:
            result = SourceResult(name=name, skipped_reason=f"{name} failed: {exc}")
        summary.sources[name] = len(result.postings)
        summary.not_supported += len(result.not_supported)
        if result.skipped_reason:
            summary.notes.append(result.skipped_reason)
        summary.notes.extend(result.notes)
        results.append(result)

    touched: dict[str, tuple[str, str]] = {}  # dedupe key -> (label, ghost risk)
    with_body: set[str] = set()
    dry_ids = 0
    for result in results:
        for raw in result.postings:
            outcome = normalize(raw, rules, active)
            if isinstance(outcome, Skipped):
                summary.skipped += 1
                log.debug("skipped %s: %s", raw.title, outcome.reason)
                continue
            job = outcome
            row = index.get(job.dedupe_key)
            if row is None:
                plan = create_plan(job, today)
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
                touched[job.dedupe_key] = (_label(job), plan["Ghost job risk"])
                continue

            plan_update = update_plan(row, job, today)
            if repo:
                repo.update(row.page_id, plan_update.props)
                if job.description and row.page_id not in with_body:
                    if not repo.has_body(row.page_id):
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

    high = sorted(label for label, risk in touched.values() if risk == "High")
    summary.high_ghost = len(high)
    summary.high_ghost_jobs = high
    return summary
