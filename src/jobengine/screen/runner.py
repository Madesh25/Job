"""Screening run (spec section 3): read Unscreened rows, extract, gate, tier, write."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from jobengine import http
from jobengine.bot_state import BotState, FakeBotState, bot_state_for
from jobengine.config_store import ConfigStore
from jobengine.ind_register import IndRegister, load_register
from jobengine.llm import AnthropicLLM, FakeLLM, LLMAuthError, LLMClient, LLMError
from jobengine.notion_repo import (
    FakeJobsRepo,
    JobsRepo,
    NotionClient,
    jobs_repo_for,
    select_filter,
)
from jobengine.parallel import run_all
from jobengine.reference import Reference
from jobengine.safety import notion_write_target
from jobengine.screen import daily, visa, waiting
from jobengine.screen.extract import extract
from jobengine.screen.gates import (
    APPLIED_STATUSES,
    MAX_YEARS,
    pre_gate,
    run_gates,
    unbacked_tools,
)
from jobengine.screen.models import (
    Extraction,
    JobRow,
    ScreenResult,
    ScreenSummary,
    join_gaps,
)
from jobengine.screen.tiering import build_matrix, contract_type, tier
from jobengine.settings import ROOT_DIR, Settings
from jobengine.sweep.dedupe import parse_posting_ids
from jobengine.sweep.normalize import experience, years_low, years_required

log = logging.getLogger("jobengine.screen")

FIXTURES = ROOT_DIR / "fixtures" / "screen"
FAKE_TODAY = date(2026, 10, 1)
DESCRIPTION_HEADER = "Description source:"
SNIPPET_MARK = "(snippet only)"
SECTION_PREFIX = "Screening ("
FULL_MIN_CHARS = 600
REVIEW_VERDICTS = ("Apply high", "Apply normal", "Apply low", "Needs review")
SPONSORSHIP_LABELS = {
    "stated_yes": "Stated yes",
    "stated_no": "Stated no",
    "not_mentioned": "Not mentioned",
}


class ScreenError(Exception):
    """Screening cannot run at all (for example NOTION_TOKEN is missing)."""


@dataclass
class ScreenDeps:
    """Everything screening reads from or writes to."""

    config: Callable[[], ConfigStore]
    reference: Callable[[], Reference]
    repo: JobsRepo | None  # None: no Job Opportunities target in this env
    llm: Callable[[ConfigStore], LLMClient]
    get_text: Callable[[str], str]  # for the IND register page
    write: bool = True
    notes: list[str] = field(default_factory=list)
    state: BotState | None = None  # holds the daily screening count


def fake_deps(s: Settings, base: Path = FIXTURES, write: bool = True) -> ScreenDeps:
    """Fixture rows, descriptions, reference data, LLM answers and IND register. No network."""
    repo = None
    if notion_write_target("job_opportunities", s):
        repo = FakeJobsRepo.from_fixture(base / "job_opportunities_seed.json")
        for page_id, row in repo.rows.items():
            desc = base / "descriptions" / f"{page_id}.txt"
            if desc.exists() and not row.get("body"):
                header, _, text = desc.read_text(encoding="utf-8").partition("\n")
                row["body"] = [header, *_chunks(text.strip(), 2000, 90)]

    def get_text(url: str) -> str:
        return (ROOT_DIR / "fixtures" / "ind_register.html").read_text(encoding="utf-8")

    llm = FakeLLM()  # one client, so a batch sent by one run can be collected by the next
    return ScreenDeps(
        config=ConfigStore.fake,
        reference=Reference.fake,
        repo=repo,
        llm=lambda config: llm,
        get_text=get_text,
        write=write,
        state=FakeBotState(),
    )


def real_deps(s: Settings, write: bool = True) -> ScreenDeps:
    if not s.notion_token:
        raise ScreenError("screening cannot run: NOTION_TOKEN missing")
    client = NotionClient(s.notion_token, s)
    return ScreenDeps(
        config=lambda: ConfigStore.load(client, s),
        reference=lambda: Reference.load(client, s),
        repo=jobs_repo_for(s, client),
        llm=lambda config: AnthropicLLM(s, config),
        get_text=lambda url: http.get_text(url, s=s),
        write=write,
        state=bot_state_for(s, client),
    )


# ---------------------------------------------------------------- rows and descriptions


def _chunks(text: str, size: int, limit: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)][:limit]


def job_row(page_id: str, values: dict[str, Any]) -> JobRow:
    years = values.get("Years required")
    return JobRow(
        page_id=page_id,
        company=values.get("Company") or "",
        role=values.get("Role") or "",
        country=values.get("Country"),
        city=values.get("City"),
        dedupe_key=values.get("Dedupe key"),
        status=values.get("Status"),
        screen_verdict=values.get("Screen verdict"),
        years_required=years_low(years),
        expires=values.get("Expires"),
        posted_date=values.get("Posted date"),
        ghost_risk=values.get("Ghost job risk"),
        salary=values.get("Salary"),
        visa_flags=tuple(values.get("Visa flags") or ()),
        url=values.get("URL"),
        board=values.get("Board"),
        posting_ids=values.get("Posting IDs"),
        sponsorship=values.get("Sponsorship"),
        contract_type=values.get("Contract type"),
        gaps=values.get("Gaps"),
        swept_date=values.get("Swept date"),
    )


def description(blocks: list[str], full_min: int = FULL_MIN_CHARS) -> tuple[str, str]:
    """The newest description in a page body and its kind: full, snippet or none.

    A description starts at a "Description source:" line and runs until the next one or a
    "Screening (" section. A pasted JD written after a snippet therefore wins.
    """
    start = None
    for i, text in enumerate(blocks):
        if text.startswith(DESCRIPTION_HEADER):
            start = i
    if start is None:
        return "", "none"
    parts = []
    for text in blocks[start + 1:]:
        if text.startswith(DESCRIPTION_HEADER) or text.startswith(SECTION_PREFIX):
            break
        parts.append(text)
    body = "".join(parts).strip()
    if not body:
        return "", "none"
    if SNIPPET_MARK in blocks[start] or len(body) < full_min:
        return body, "snippet"
    return body, "full"


# ---------------------------------------------------------------- what gets written


def language_required(ext: Extraction) -> str:
    langs = [(lang.language.strip().lower(), lang.level) for lang in ext.languages]
    for name, label in (("polish", "Polish"), ("dutch", "Dutch")):
        if (name, "mandatory") in langs:
            return label
    for name, label in (("polish", "Polish preferred"), ("dutch", "Dutch preferred")):
        if (name, "preferred") in langs:
            return label
    if any(name == "english" for name, _ in langs):
        return "English"
    return "Other" if langs else "Unknown"


def tool_terms(ext: Extraction) -> list[str]:
    terms: list[str] = []
    for req in ext.mandatory_requirements:
        if req.kind == "tool":
            terms.extend(t for t in (req.terms or [req.text]) if t not in terms)
    return terms


def body_section(result: ScreenResult, today: date) -> list[str]:
    lines = [f"Screening (V16, {today.isoformat()})"]
    for item in result.matrix:
        lines.append(f"{item.strength} | {item.text} | {item.backing or 'none'}")
    ext = result.extraction
    if ext and ext.specific_details:
        lines.append("Specific details: " + " ; ".join(ext.specific_details))
    verdict = result.verdict
    if result.skip_reason:
        verdict += f" ({result.skip_reason})"
    lines.append(f"Verdict: {verdict}")
    lines.extend(f"Note: {note}" for note in result.notes)
    return lines


def years_update(row: JobRow, jd: str) -> dict[str, Any]:
    """{"Years required": ...} when the description asks for more years than the cell says
    (the cell may come from a snippet that showed only the smaller requirement)."""
    years = years_required(jd)
    if years is None or (row.years_required is not None and years <= row.years_required):
        return {}
    return {"Years required": experience(jd) or f"{years} years"}


def plan_props(
    result: ScreenResult, row: JobRow, status_from: tuple[str, ...] = ("New",)
) -> dict[str, Any]:
    """Section 8 properties. Status becomes Screened only when it is in `status_from`."""
    ext = result.extraction or Extraction()
    props: dict[str, Any] = {"Screen verdict": result.verdict}
    if row.status in status_from or row.status is None:
        props["Status"] = "Screened"
    if result.skip_reason:
        props["Skip reason"] = result.skip_reason
    props["Language required"] = language_required(ext)
    props["Contract type"] = contract_type(row.country, ext)
    props["Sponsorship"] = SPONSORSHIP_LABELS.get(ext.sponsorship_value, "Not mentioned")
    if ext.work_mode.value in ("Remote", "Hybrid", "Office"):
        props["Work mode"] = ext.work_mode.value
    if ext.expires_date:
        props["Expires"] = ext.expires_date
    if not row.salary and ext.salary_text.value:
        props["Salary"] = str(ext.salary_text.value)
    if row.years_required is None and ext.years is not None:
        props["Years required"] = f"{ext.years}+ years"  # the minimum the LLM read
    merged = list(row.visa_flags) + [f for f in result.visa_flags if f not in row.visa_flags]
    if merged != list(row.visa_flags):
        props["Visa flags"] = merged
    terms = tool_terms(ext)
    if terms:
        props["Tech stack"] = terms
    if result.gaps:
        props["Gaps"] = join_gaps(result.gaps)
    return props


# ---------------------------------------------------------------- the run


@dataclass
class _Run:
    s: Settings
    deps: ScreenDeps
    today: date
    config: ConfigStore
    ref: Reference
    llm: LLMClient
    applied: list[JobRow]
    _register: IndRegister | None = None
    _register_tried: bool = False
    # Rows are screened on several threads: the IND register is loaded once.
    _lock: threading.Lock = field(default_factory=threading.Lock)
    notes: list[str] = field(default_factory=list)

    def register(self) -> IndRegister | None:
        with self._lock:
            return self._load_register()

    def _load_register(self) -> IndRegister | None:
        if not self._register_tried:
            self._register_tried = True
            url = self.config.get("ind_register.url")
            try:
                if not url:
                    raise ValueError("Config ind_register.url is missing")
                self._register = load_register(url, self.deps.get_text)
                self._register.add_aliases(self.config.get("ind_register.aliases"))
                log.info("IND register: %d organisations", len(self._register.names))
            except (http.HttpError, ValueError) as exc:
                log.warning("IND register unavailable: %s", exc)
                self.notes.append("IND register unavailable")
        return self._register

    def needs_register(self, row: JobRow) -> bool:
        if row.country != "Netherlands":
            return False
        company = self.ref.company(row.company)
        return not (company and company.ind_sponsor in ("Verified", "Not listed"))

    def screen(self, row: JobRow, status_from: tuple[str, ...]) -> ScreenResult:
        prepared = self.prepare(row, status_from)
        if isinstance(prepared, ScreenResult):
            return prepared
        ext, dropped = extract(self.llm, title=row.role, company=row.company,
                               country=row.country or "", jd=prepared.jd, key=row.page_id)
        return self.finish(row, prepared, ext, dropped, status_from)

    def prepare(self, row: JobRow, status_from: tuple[str, ...]) -> ScreenResult | Prepared:
        """Everything before the LLM: read the description and run the free checks. A
        ScreenResult means the row is done (no description yet, or skipped for free)."""
        repo = self.deps.repo
        assert repo is not None
        full_min = int(self.s.screening.get("full_min_chars", FULL_MIN_CHARS))
        jd, kind = description(repo.read_body(row.page_id), full_min)
        if kind == "none":
            return ScreenResult(page_id=row.page_id, verdict="Unscreened", description_kind=kind)
        limit = int(self.s.screening.get("max_years_required", MAX_YEARS))
        years_fix = years_update(row, jd)
        early = pre_gate(row, years_required(jd), limit, jd)
        if early:  # skipped for free, no tokens spent
            result = ScreenResult(page_id=row.page_id, verdict="Skip", skip_reason=early.reason,
                                  description_kind=kind, llm_used=False,
                                  notes=[f"gate {early.gate}: {early.detail} (no AI used)"])
            if self.deps.write:
                props = {key: value for key, value in plan_props(result, row, status_from).items()
                         if key in ("Screen verdict", "Status", "Skip reason")} | years_fix
                repo.update(row.page_id, props)
                repo.append_body(row.page_id, body_section(result, self.today))
            return result
        return Prepared(jd=jd, kind=kind, years_fix=years_fix, limit=limit)

    def finish(
        self, row: JobRow, prepared: Prepared, ext: Extraction, dropped: list[str],
        status_from: tuple[str, ...],
    ) -> ScreenResult:
        """Everything after the LLM: gates, tier, and the Notion writes."""
        repo = self.deps.repo
        assert repo is not None
        if dropped:
            log.info("%s: dropped %d unquoted values: %s", row.page_id, len(dropped),
                     ", ".join(dropped))
        matrix = build_matrix(ext, self.ref)
        result = ScreenResult(page_id=row.page_id, verdict="Skip", matrix=matrix,
                              extraction=ext, description_kind=prepared.kind,
                              tech_terms=tool_terms(ext))
        hit = run_gates(row, ext, self.ref, self.applied, self.today, prepared.limit,
                        tech_skips=bool(self.s.screening.get("tech_mismatch_skips", False)))
        if hit:
            result.skip_reason = hit.reason
            result.gaps = list(hit.gaps) or [m.text for m in matrix if m.strength == "Gap"]
            result.notes.append(f"gate {hit.gate}: {hit.detail}")
            if visa.is_agency(row.company, ext, self.config):  # marked even when skipped
                result.visa_flags = [visa.agency_flag(row.country)]
        else:
            register = self.register() if self.needs_register(row) else None
            checks = visa.visa_checks(row, ext, self.ref, self.config, register)
            flags = checks.flags
            tiering = tier(row, ext, self.ref, matrix, flags, prepared.kind)
            result.verdict = tiering.verdict
            result.bottom = tiering.bottom
            result.employer = tiering.employer
            result.visa_flags = flags + [f for f in tiering.flags if f not in flags]
            result.gaps = [m.text for m in matrix if m.strength == "Gap"]
            named = " | ".join(result.gaps).lower()
            missing = [t for t in unbacked_tools(ext, self.ref) if t.lower() not in named]
            result.gaps.extend(missing)  # required tools you do not have: gaps, not a skip
            result.notes.extend(checks.notes)
            result.notes.extend(tiering.notes)
        if self.deps.write:
            repo.update(row.page_id, plan_props(result, row, status_from) | prepared.years_fix)
            repo.append_body(row.page_id, body_section(result, self.today))
        return result


@dataclass(frozen=True)
class Prepared:
    """A row that passed the free checks and waits for the LLM."""

    jd: str
    kind: str
    years_fix: dict[str, Any]
    limit: int


def _start(s: Settings, deps: ScreenDeps, today: date) -> _Run:
    config = deps.config()
    try:
        llm = deps.llm(config)
    except LLMError as exc:
        raise ScreenError(str(exc)) from None
    assert deps.repo is not None
    applied = [job_row(pid, v) for pid, v in
               deps.repo.query_rows(select_filter("Status", *sorted(APPLIED_STATUSES)))]
    return _Run(s, deps, today, config, deps.reference(), llm, applied)


BATCH_STATE_KEY = "screen.batch"  # screen/batch.py: the batch that waits to be collected


def _batch_rows(deps: ScreenDeps) -> set[str]:
    pending = deps.state.get(BATCH_STATE_KEY) if deps.state is not None else None
    return set((pending or {}).get("rows") or [])


def unscreened_rows(deps: ScreenDeps, today: date | None = None) -> list[JobRow]:
    """Unscreened rows without the rows of a waiting batch: today's best new jobs first (the
    /fetch match score, sweep/best.py), then the others newest posting first."""
    from jobengine.sweep import best

    assert deps.repo is not None
    waiting = _batch_rows(deps)
    rows = [job_row(pid, v) for pid, v in
            deps.repo.query_rows(select_filter("Screen verdict", "Unscreened"))
            if pid not in waiting]
    ranked = best.scores(deps.state, today) if today else {}
    rows.sort(key=lambda r: r.posted_date or date.min, reverse=True)
    rows.sort(key=lambda r: ranked.get(r.page_id.replace("-", ""), float("-inf")),
              reverse=True)  # stable: equal scores keep the newest first
    return rows


def _label(row: JobRow) -> str:
    return f"{row.company}, {row.role}"


def screen_pending(
    s: Settings, deps: ScreenDeps, today: date, progress: Callable[[str], None] | None = None,
    limit: int | None = None,
) -> ScreenSummary:
    """Screen Unscreened rows, newest posting first, up to the daily limit.

    `limit` (the CLI --limit) caps this run instead of what is left of today's limit."""
    summary = ScreenSummary()
    if deps.repo is None:
        summary.errors.append("DRY RUN: no Job Opportunities target here, nothing screened")
        return summary
    used = daily.used_today(deps.state, today)
    room = limit if limit is not None else max(0, daily.daily_limit(s) - used)
    rows = unscreened_rows(deps, today)
    in_batch = len(_batch_rows(deps))
    if in_batch:
        summary.errors.append(f"{in_batch} jobs wait in a half-price batch: /screen collect "
                              "saves them.")
    log.info("%d Unscreened rows, screening at most %d", len(rows), room)
    if room <= 0:
        summary.errors.append(_limit_note(s, used, len(rows), limit))
        return summary
    run = _start(s, deps, today)
    sent = 0  # rows that went to the LLM
    done = 0
    stopped = False
    workers = max(1, int(s.screening.get("workers", 4)))

    def one(row: JobRow) -> tuple[ScreenResult | None, Exception | None]:
        try:
            return run.screen(row, ("New",)), None
        except (LLMError, http.HttpError) as exc:
            return None, exc

    try:
        pos = 0
        while pos < len(rows) and sent < room and not stopped:
            # One job first (a refused key then costs one call), then `workers` at a time,
            # never more than what is left of today's limit.
            size = 1 if sent == 0 else min(workers, room - sent)
            batch = rows[pos:pos + size]
            pos += len(batch)
            for row, (result, exc) in zip(batch, run_all(one, batch, workers), strict=True):
                done += 1
                if progress and done % 10 == 0:
                    progress(f"Screening: {done} of {min(len(rows), room)} jobs checked")
                if isinstance(exc, LLMAuthError):  # every other job would fail the same way
                    if not stopped:
                        summary.errors.append(f"Screening stopped: {exc}")
                    stopped = True
                    continue
                if exc is not None or result is None:
                    summary.errors.append(f"Could not screen {_label(row)}: {exc}")
                    continue
                if result.description_kind != "none" and result.llm_used:
                    sent += 1
                if result.verdict == "Unscreened":
                    summary.waiting_for_jd += 1
                summary.add(result)
    finally:
        if deps.write and sent:
            daily.record(deps.state, today, used + sent)
    if done < len(rows) and not stopped:
        summary.errors.append(_limit_note(s, used + sent, len(rows) - done, limit))
    summary.usage_line = _usage_line(run)
    summary.errors.extend(dict.fromkeys(run.notes))
    if not deps.write:
        summary.errors.append("--no-write: nothing was written")
    return summary


def find_row(repo: JobsRepo, ref: str) -> JobRow | None:
    """A row by page ID, URL, or posting ID (for example linkedin:4012345678)."""
    ref = ref.strip()
    if ref.startswith("http"):
        where = {"property": "URL", "url": {"equals": ref}}
    elif ":" in ref:
        where = {"property": "Posting IDs", "rich_text": {"contains": ref}}
    else:
        values = repo.get_values(ref)
        return job_row(ref, values) if values else None
    for pid, values in repo.query_rows(where):
        if ref.startswith("http") or ref in parse_posting_ids(values.get("Posting IDs")):
            return job_row(pid, values)
    return None


def _limit_note(s: Settings, used: int, left: int, limit: int | None) -> str:
    cap = f"--limit {limit}" if limit is not None else f"daily limit {daily.daily_limit(s)}"
    return (f"{cap} reached ({used} screened today): {left} Unscreened rows wait for the "
            "next run. /screen <url> screens one job now.")


def screen_one(s: Settings, deps: ScreenDeps, today: date, ref: str) -> ScreenSummary:
    """Re-screen one row whatever its verdict. Status becomes Screened only from New or
    Screened, so a row past Screened never moves backwards."""
    summary = ScreenSummary()
    if deps.repo is None:
        summary.errors.append("DRY RUN: no Job Opportunities target here, nothing screened")
        return summary
    row = find_row(deps.repo, ref)
    if row is None:
        summary.errors.append(f"No Job Opportunities row found for {ref}")
        return summary
    run = _start(s, deps, today)
    try:
        result = run.screen(row, ("New", "Screened"))
    except (LLMError, http.HttpError) as exc:
        summary.errors.append(f"Could not screen {_label(row)}: {exc}")
        return summary
    # Your ask: counted, never blocked.
    if deps.write and result.description_kind != "none" and result.llm_used:
        daily.record(deps.state, today, daily.used_today(deps.state, today) + 1)
    if result.verdict == "Unscreened":
        summary.waiting_for_jd += 1
    summary.add(result)
    summary.usage_line = _usage_line(run)
    summary.errors.extend(dict.fromkeys(run.notes))
    if not deps.write:
        summary.errors.append("--no-write: nothing was written")
    return summary


def _usage_line(run: _Run) -> str | None:
    usage = getattr(run.llm, "usage", None)
    return usage.line() if usage is not None else None


def pending_rows(repo: JobsRepo) -> list[JobRow]:
    """Rows waiting for your decision: Status Screened and an Apply or Needs review verdict."""
    rows = repo.query_rows({"and": [select_filter("Status", "Screened"),
                                    select_filter("Screen verdict", *REVIEW_VERDICTS)]})
    return [job_row(pid, v) for pid, v in rows]


def waiting_for_jd(repo: JobsRepo, state: Any = None) -> list[JobRow]:
    """Unscreened rows that wait for /jd: LinkedIn rows (LinkedIn is never fetched) and,
    with `state`, rows whose page gave no description (screen/waiting.py: Pracuj.pl and
    other sites that block the bot). LinkedIn first, then by board."""
    if state is None:
        rows = repo.query_rows({"and": [select_filter("Screen verdict", "Unscreened"),
                                        select_filter("Board", "LinkedIn")]})
        return [job_row(pid, v) for pid, v in rows]
    known = set(waiting.noted(state))
    rows = [job_row(pid, v)
            for pid, v in repo.query_rows(select_filter("Screen verdict", "Unscreened"))
            if v.get("Board") == "LinkedIn" or pid in known]
    return sorted(rows, key=lambda r: (r.board != "LinkedIn", r.board or ""))
