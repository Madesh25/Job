"""Build, preview, finalise (spec sections 2, 7 and 8).

build_resume: plan (LLM, stage tailor) -> integrity gate (with retries) -> fit -> render ->
Resume Log row (one per revision, plan in its body) -> Job Status "Resume built".
finalise: re-render from the stored plan of the approved row, save the PDF (Drive, or out/ in
DRY_RUN), tick Approved. mark_applied: Status "Applied" from "Resume built" only.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from jobengine import http
from jobengine.config_store import ConfigStore
from jobengine.drive_client import Drive, DriveError, FakeDrive, GoogleDrive
from jobengine.llm import AnthropicLLM, FakeLLM, LLMClient, LLMError
from jobengine.notion_repo import (
    FakeJobsRepo,
    FakeResumeLogRepo,
    JobsRepo,
    NotionClient,
    ResumeLogRepo,
    jobs_repo_for,
    resume_log_for,
)
from jobengine.reference import Reference
from jobengine.resume import gate
from jobengine.resume.master import (
    MasterError,
    fake_blocks,
    fetch_blocks,
    load_master,
    profile_from_config,
    render_html,
)
from jobengine.resume.models import MasterResume, Plan
from jobengine.resume.plan import JobContext, PlanError, make_plan, parse_plan
from jobengine.resume.render import (
    FitError,
    Measurer,
    RenderError,
    check_measure,
    filename,
    fit,
    measure_html,
    render_pdf,
)
from jobengine.resume.sections import SectionError, SectionPlan, SectionRow, fake_rows, load_rows
from jobengine.safety import drive_write_allowed
from jobengine.screen.runner import description
from jobengine.settings import ROOT_DIR, Settings
from jobengine.sweep.normalize import canon_company

log = logging.getLogger("jobengine.resume")

FIXTURES = ROOT_DIR / "fixtures" / "resume"
BUILDABLE_VERDICTS = ("Apply high", "Apply normal", "Apply low", "Needs review")
BUILDABLE_STATUSES = ("Approved", "Resume built")
CAPTION_MAX = 1024
RULES_FAILED = (
    "Resume could not be built within the rules: {errors}. The job stays Approved; tap "
    "Rebuild or reply with a correction."
)
APPLY_TEXT = "Resume approved and saved. Apply here: {url}"
NEWER_TEXT = "A newer revision exists"


@dataclass
class ResumeDeps:
    s: Settings
    config: Callable[[], ConfigStore]
    reference: Callable[[], Reference]
    jobs: JobsRepo | None
    resume_log: ResumeLogRepo | None
    blocks: Callable[[], list[dict[str, Any]]]  # the Resume Build Spec page
    sections: Callable[[], list[SectionRow]]
    llm: Callable[[ConfigStore], LLMClient]
    drive: Callable[[], Drive]
    measure: Measurer = measure_html
    render: Callable[[str], bytes] = render_pdf
    out_dir: Path = ROOT_DIR / "out"
    today: Callable[[], date] = date.today
    write: bool = True  # False: --no-write, render only


@dataclass
class BuildOutcome:
    status: str  # built | existing | refused | failed | correction_refused
    message: str
    job_id: str
    company: str = ""
    role: str = ""
    revision: int | None = None
    log_id: str | None = None
    pdf: bytes | None = None
    filename: str | None = None
    caption: str | None = None
    plan: Plan | None = None
    fill: float | None = None
    changes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("built", "existing")


@dataclass
class FinaliseOutcome:
    status: str  # approved | newer | failed
    message: str
    job_id: str | None = None
    job_url: str | None = None
    file: str | None = None
    latest: BuildOutcome | None = None


def short_ref(log_id: str) -> str:
    return "RL-" + log_id.replace("-", "")[:8]


def hex_id(page_id: str) -> str:
    return page_id.replace("-", "")


# ---------------------------------------------------------------- deps


def fake_deps(
    s: Settings,
    jobs: JobsRepo | None = None,
    out_dir: Path | None = None,
    write: bool = True,
) -> ResumeDeps:
    """Fixtures for Notion, the LLM (unknown keys give the unchanged plan) and Drive."""
    if jobs is None:
        jobs = FakeJobsRepo.from_fixture(FIXTURES / "job_opportunities_seed.json")
    drive = FakeDrive()
    return ResumeDeps(
        s=s,
        config=ConfigStore.fake,
        reference=Reference.fake,
        jobs=jobs,
        resume_log=FakeResumeLogRepo(),
        blocks=fake_blocks,
        sections=fake_rows,
        llm=lambda config: FakeLLM(default={}),
        drive=lambda: drive,
        out_dir=out_dir or ROOT_DIR / "out",
        write=write,
    )


def real_deps(s: Settings, write: bool = True) -> ResumeDeps:
    if not s.notion_token:
        raise MasterError("resume builder cannot run: NOTION_TOKEN missing")
    client = NotionClient(s.notion_token, s)
    page = s.notion_pages.get("resume_build_spec", "")
    return ResumeDeps(
        s=s,
        config=lambda: ConfigStore.load(client, s),
        reference=lambda: Reference.load(client, s),
        jobs=jobs_repo_for(s, client),
        resume_log=resume_log_for(s, client),
        blocks=lambda: fetch_blocks(client, page),
        sections=lambda: load_rows(client, s),
        llm=lambda config: AnthropicLLM(s, config),
        drive=lambda: GoogleDrive.from_settings(s),
        write=write,
    )


# ---------------------------------------------------------------- helpers


def _setting(deps: ResumeDeps, config: ConfigStore, key: str, default: float) -> float:
    value = config.get(f"resume.{key}")
    try:
        return float(value) if value else float(deps.s.resume.get(key, default))
    except ValueError:
        return float(deps.s.resume.get(key, default))


def _specific_details(body: list[str]) -> list[str]:
    for line in reversed(body):
        if line.startswith("Specific details:"):
            return [d.strip() for d in line.split(":", 1)[1].split(";") if d.strip()]
    return []


def _job_context(page_id: str, values: dict[str, Any], body: list[str]) -> JobContext:
    jd, _ = description(body, full_min=0)
    return JobContext(
        page_id=page_id, company=values.get("Company") or "", role=values.get("Role") or "",
        country=values.get("Country"), jd=jd, specific_details=_specific_details(body),
        gaps=values.get("Gaps"),
    )


def _sorted_rows(deps: ResumeDeps, job_id: str) -> list[tuple[str, dict[str, Any]]]:
    assert deps.resume_log is not None
    rows = deps.resume_log.rows_for_job(job_id)
    return sorted(rows, key=lambda r: int(r[1].get("Revision") or 0))


def stored_plan(deps: ResumeDeps, log_id: str, master: MasterResume) -> Plan:
    assert deps.resume_log is not None
    for text in deps.resume_log.read_body(log_id):
        if text.lstrip().startswith("{"):
            return parse_plan(json.loads(text), master)
    raise PlanError("the Resume Log row has no stored plan")


def _file_name(deps: ResumeDeps, config: ConfigStore, job_id: str, values: dict[str, Any]) -> str:
    template = config.get("resume.filename")
    company, role = values.get("Company") or "", values.get("Role") or ""
    if deps.resume_log is not None and deps.jobs is not None:
        mine = canon_company(company)
        for _, row in deps.resume_log.approved_rows():
            for other in row.get("Job") or []:
                if hex_id(other) == hex_id(job_id):
                    continue
                other_values = deps.jobs.get_values(other) or {}
                if canon_company(other_values.get("Company")) == mine:
                    return filename(template, company, role)
    return filename(template, company)


def caption(
    revision: int, values: dict[str, Any], fill: float, changes: gate.Changes, plan: Plan,
    log_id: str | None,
) -> str:
    skills = ", ".join(changes.skills) or "unchanged"
    lines = [
        f"Resume r{revision} for {values.get('Company')}, {values.get('Role')}",
        f"Fill {fill:.1f}% | +{changes.words} words in {changes.bullets} bullets | "
        f"skills: {skills}",
    ]
    if plan.gaps_reported:
        lines.append(f"Gaps not added: {', '.join(plan.gaps_reported)}")
    if log_id:
        lines.append(f"Ref {short_ref(log_id)}")
    lines.append("Reply to this message with a correction, or tap a button.")
    text = "\n".join(lines)
    if len(text) > CAPTION_MAX:
        cut = len(text) - CAPTION_MAX + 3
        lines[1] = lines[1][: max(len(lines[1]) - cut, 20)] + "..."
        text = "\n".join(lines)
    return text


def body_blocks(plan: Plan, changes: gate.Changes) -> list[tuple[str, ...]]:
    blocks: list[tuple[str, ...]] = [
        ("heading_2", "Plan"),
        ("code", json.dumps(plan.to_json(), indent=1, ensure_ascii=False), "json"),
        ("heading_2", "Changes"),
    ]
    blocks += [("paragraph", line) for line in changes.lines or ["no changes"]]
    blocks += [("heading_2", "Gaps reported"),
               ("paragraph", ", ".join(plan.gaps_reported) or "none")]
    return blocks


@dataclass
class _Context:
    config: ConfigStore
    reference: Reference
    master: MasterResume
    sections: SectionPlan
    values: dict[str, Any]
    fill_min: float
    fill_max: float


def _context(deps: ResumeDeps, job_id: str, values: dict[str, Any]) -> _Context:
    from jobengine.resume.sections import resolve

    config = deps.config()
    master = load_master(deps.blocks(), config)
    sections = resolve(deps.sections(), values.get("Country"), config)
    return _Context(
        config=config, reference=deps.reference(), master=master, sections=sections,
        values=values, fill_min=_setting(deps, config, "fill_min", 88),
        fill_max=_setting(deps, config, "fill_max", 96),
    )


def connector_words(deps: ResumeDeps, config: ConfigStore) -> list[str]:
    """Config resume.gate.connector_words, else the settings default (comma or space list)."""
    raw = config.get("resume.gate.connector_words") or deps.s.resume.get("connector_words")
    words = [w for w in re.split(r"[,\s]+", str(raw or "")) if w]
    return words or sorted(gate.CONNECTOR_WORDS)


def _gate(deps: ResumeDeps, ctx: _Context, plan: Plan) -> list[str]:
    return gate.check(
        ctx.master, plan, ctx.reference, sections=ctx.sections,
        connectors=connector_words(deps, ctx.config),
        max_per_bullet=int(deps.s.resume.get("max_words_per_bullet", 8)),
        max_total=int(deps.s.resume.get("max_words_total", 25)),
    )


def _render_checked(deps: ResumeDeps, ctx: _Context, plan: Plan) -> tuple[str, bytes, float]:
    """Render a final plan and run the Build Spec checks. Raises RenderError."""
    html = render_html(ctx.master, plan, ctx.sections)
    pdf = deps.render(html)
    m = deps.measure(html)
    errors = check_measure(m, profile_from_config(ctx.config), ctx.fill_min, ctx.fill_max)
    if errors:
        raise RenderError("; ".join(errors))
    return html, pdf, m.fill


def _save_local(deps: ResumeDeps, name: str, pdf: bytes) -> Path:
    deps.out_dir.mkdir(parents=True, exist_ok=True)
    path = deps.out_dir / name
    path.write_bytes(pdf)
    return path


# ---------------------------------------------------------------- build


def _load_job(deps: ResumeDeps, job_id: str) -> tuple[dict[str, Any] | None, str | None]:
    if deps.jobs is None:
        return None, "DRY RUN: no Job Opportunities target here, nothing built."
    values = deps.jobs.get_values(job_id)
    if not values:
        return None, f"No Job Opportunities row {job_id}."
    verdict, status = values.get("Screen verdict"), values.get("Status")
    if verdict not in BUILDABLE_VERDICTS:
        return None, f"No resume for this job: its verdict is {verdict or 'unknown'}."
    if status not in BUILDABLE_STATUSES:
        return None, (f"No resume for this job: its Status is {status or 'empty'}, a resume is "
                      "built only after you approve it in /pending.")
    return values, None


def preview_existing(deps: ResumeDeps, job_id: str) -> BuildOutcome | None:
    """The latest revision for a job, re-rendered from its stored plan, or None."""
    values, refused = _load_job(deps, job_id)
    if deps.resume_log is None or values is None:
        return None
    rows = _sorted_rows(deps, job_id)
    if not rows:
        return None
    log_id, row = rows[-1]
    ctx = _context(deps, job_id, values)
    plan = stored_plan(deps, log_id, ctx.master)
    html, pdf, fill = _render_checked(deps, ctx, plan)
    changes = gate.summarise(ctx.master, plan)
    revision = int(row.get("Revision") or 1)
    name = (row.get("Resume name") or "").rsplit(" r", 1)[0]
    return BuildOutcome(
        status="existing", message="Latest resume preview.", job_id=job_id,
        company=values.get("Company") or "", role=values.get("Role") or "", revision=revision,
        log_id=log_id, pdf=pdf, filename=name, plan=plan, fill=fill, changes=changes.lines,
        caption=caption(revision, values, fill, changes, plan, log_id),
    )


def build_resume(
    deps: ResumeDeps,
    job_id: str,
    *,
    correction: str | None = None,
    force: bool = False,
) -> BuildOutcome:
    """Build the next revision. Without `force` (the first Approve), an existing build is
    shown again instead of building another one."""
    values, refused = _load_job(deps, job_id)
    if values is None:
        return BuildOutcome(status="refused", message=refused or "", job_id=job_id)
    company, role = values.get("Company") or "", values.get("Role") or ""

    def failed(message: str) -> BuildOutcome:
        log.warning("resume for %s failed: %s", job_id, message)
        return BuildOutcome(status="failed", message=message, job_id=job_id, company=company,
                            role=role)

    try:
        existing = _sorted_rows(deps, job_id) if deps.resume_log is not None else []
        if existing and not force:
            latest = preview_existing(deps, job_id)
            if latest:
                return latest
        ctx = _context(deps, job_id, values)
    except (MasterError, SectionError, PlanError, RenderError) as exc:
        return failed(str(exc))
    except http.HttpError as exc:
        return failed(f"Notion could not be read: {exc}")

    revision = (int(existing[-1][1].get("Revision") or 0) if existing else 0) + 1
    max_revisions = int(_setting(deps, ctx.config, "max_revisions", 5))
    if revision > max_revisions:
        return BuildOutcome(
            status="refused", job_id=job_id, company=company, role=role,
            message=(f"This job already has {max_revisions} revisions. Fix the rules (Skills "
                     "Inventory, Term Map) or skip the job."))

    body = deps.jobs.read_body(job_id) if deps.jobs else []
    job = _job_context(job_id, values, body)
    if not job.jd:
        return failed("No job description on the job page. Paste it with /jd first.")
    previous = None
    if existing:
        try:
            previous = stored_plan(deps, existing[-1][0], ctx.master)
        except PlanError:
            previous = None

    try:
        llm = deps.llm(ctx.config)
    except LLMError as exc:
        return failed(str(exc))
    retries = int(deps.s.resume.get("gate_retries", 2))
    errors: list[str] = []
    plan: Plan | None = None
    for attempt in range(retries + 1):
        key = f"{job_id}-r{revision}" + (f"-a{attempt}" if attempt else "")
        try:
            plan = make_plan(llm, ctx.master, job, ctx.reference, correction=correction,
                             previous=plan or previous, errors=errors or None, key=key)
        except (LLMError, PlanError) as exc:
            return failed(str(exc))
        if plan.correction_refused:
            return BuildOutcome(status="correction_refused", job_id=job_id, company=company,
                                role=role, message=f"Correction not applied: "
                                                   f"{plan.correction_refused}")
        errors = _gate(deps, ctx, plan)
        if not errors:
            break
        log.info("gate errors (attempt %d): %s", attempt + 1, errors)
    if errors or plan is None:
        return failed(RULES_FAILED.format(errors="; ".join(errors)))

    try:
        fitted = fit(ctx.master, plan, ctx.sections, deps.measure, ctx.fill_min, ctx.fill_max)
        plan = fitted.plan
        errors = _gate(deps, ctx, plan)
        if errors:
            return failed(RULES_FAILED.format(errors="; ".join(errors)))
        _html, pdf, fill = _render_checked(deps, ctx, plan)
    except FitError as exc:
        return failed(RULES_FAILED.format(errors=str(exc)))
    except RenderError as exc:
        return failed(str(exc))

    changes = gate.summarise(ctx.master, plan)
    name = _file_name(deps, ctx.config, job_id, values)
    log_id = None
    if deps.write and deps.resume_log is not None:
        evidence = ctx.reference.evidence_for(
            [m.jd_term for m in plan.term_mappings] + [m.used_as for m in plan.term_mappings])
        log_id = deps.resume_log.create({
            "Resume name": f"{name} r{revision}",
            "Date": deps.today(),
            "Job": [job_id],
            "Skills focus": plan.focus.skills,
            "Experience focus": plan.focus.experience,
            "Summary focus": "unchanged",
            "Diff score": changes.diff_score,
            "Page fill": f"{fill:.1f}%",
            "Engine version": f"V16 gm:{ctx.master.version}",
            "Evidence used": evidence,
            "Approved": False,
            "Revision": revision,
        }, body_blocks(plan, changes))
        update: dict[str, Any] = {"Resume": [*(values.get("Resume") or []), log_id]}
        if values.get("Status") in BUILDABLE_STATUSES:
            update["Status"] = "Resume built"
        assert deps.jobs is not None
        deps.jobs.update(job_id, update)
    else:
        path = _save_local(deps, f"{name[:-4]}_r{revision}.pdf", pdf)
        log.info("--no-write: preview saved to %s", path)
    return BuildOutcome(
        status="built", message="Resume built.", job_id=job_id, company=company, role=role,
        revision=revision, log_id=log_id, pdf=pdf, filename=name, plan=plan, fill=fill,
        changes=changes.lines, caption=caption(revision, values, fill, changes, plan, log_id),
    )


# ---------------------------------------------------------------- finalise and applied


def on_resume_approved(job_page_id: str, resume_log_page_id: str) -> None:
    """Hook for Module 05 (contact lookup). For now it only logs."""
    log.info("resume approved: job %s, resume log %s", job_page_id, resume_log_page_id)


def finalise(deps: ResumeDeps, log_id: str) -> FinaliseOutcome:
    if deps.resume_log is None or deps.jobs is None:
        return FinaliseOutcome(status="failed", message="DRY RUN: no Resume Log target here.")
    row = deps.resume_log.get(log_id)
    if not row or not row.get("Job"):
        return FinaliseOutcome(status="failed", message="That resume is no longer in Resume Log.")
    job_id = row["Job"][0]
    rows = _sorted_rows(deps, job_id)
    latest_id = rows[-1][0] if rows else log_id
    if hex_id(latest_id) != hex_id(log_id):
        try:
            latest = preview_existing(deps, job_id)
        except (MasterError, SectionError, PlanError, RenderError) as exc:
            return FinaliseOutcome(status="failed", message=str(exc), job_id=job_id)
        return FinaliseOutcome(status="newer", message=NEWER_TEXT, job_id=job_id, latest=latest)
    values = deps.jobs.get_values(job_id) or {}
    url = values.get("URL") or "(no URL on the job)"
    if row.get("Approved"):
        return FinaliseOutcome(status="approved", job_id=job_id, job_url=url, file=row.get("File"),
                               message=APPLY_TEXT.format(url=url))
    try:
        ctx = _context(deps, job_id, values)
        plan = stored_plan(deps, log_id, ctx.master)
        errors = _gate(deps, ctx, plan)
        if errors:
            return FinaliseOutcome(status="failed", job_id=job_id,
                                   message=RULES_FAILED.format(errors="; ".join(errors)))
        _html, pdf, _fill = _render_checked(deps, ctx, plan)
    except (MasterError, SectionError, PlanError, RenderError) as exc:
        return FinaliseOutcome(status="failed", message=str(exc), job_id=job_id)
    name = (row.get("Resume name") or "resume.pdf").rsplit(" r", 1)[0]
    if drive_write_allowed(deps.s):
        try:
            file_value = deps.drive().upload_pdf(name, pdf)
        except DriveError as exc:
            return FinaliseOutcome(status="failed", message=str(exc), job_id=job_id)
    else:
        path = _save_local(deps, name, pdf)
        rel = path.relative_to(ROOT_DIR) if path.is_relative_to(ROOT_DIR) else path
        file_value = f"DRY RUN: {Path(rel).as_posix()}"
        log.warning("DRY RUN: would upload %s to Drive, saved to %s instead", name, rel)
    deps.resume_log.update(log_id, {"Approved": True, "File": file_value})
    on_resume_approved(job_id, log_id)
    return FinaliseOutcome(status="approved", job_id=job_id, job_url=url, file=file_value,
                           message=APPLY_TEXT.format(url=url))


def mark_applied(deps: ResumeDeps, job_id: str) -> str:
    """"I applied": Status Applied, only from Resume built."""
    if deps.jobs is None:
        return "DRY RUN: no Job Opportunities target here."
    values = deps.jobs.get_values(job_id)
    if not values:
        return "That job is no longer in Job Opportunities."
    if values.get("Status") != "Resume built":
        return f"Already handled (Status is {values.get('Status') or 'empty'})."
    today = deps.today()
    deps.jobs.update(job_id, {"Status": "Applied", "Applied date": today,
                              "Last activity date": today})
    job = f"{values.get('Company')}, {values.get('Role')}"
    return f"Marked as applied on {today.isoformat()}: {job}."


def find_log_by_ref(deps: ResumeDeps, ref: str) -> str | None:
    """The Resume Log page for "RL-xxxxxxxx" (first 8 hex of its ID)."""
    match = re.search(r"RL-([0-9a-f]{8})", ref)
    if not match or deps.resume_log is None:
        return None
    return next((pid for pid, _ in deps.resume_log.all_rows()
                 if hex_id(pid).startswith(match.group(1))), None)


def job_for_log(deps: ResumeDeps, log_id: str) -> str | None:
    row = deps.resume_log.get(log_id) if deps.resume_log else None
    return (row.get("Job") or [None])[0] if row else None

