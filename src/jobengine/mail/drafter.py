"""create_drafts (spec section 3): one Gmail draft per contact from the approved templates,
with the Config signature and the approved resume PDF. Nothing is ever sent.

Order: Hiring, Recruiter/TA, Peer engineer, then generic mailboxes (only with Config
mail.generic_greeting_name). Every recipient goes through safety.route_recipients. In DRY_RUN
(or --no-write) no draft is created and nothing is written.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from jobengine.config_store import ConfigStore
from jobengine.contacts.jd_emails import GENERIC_MAILBOXES
from jobengine.contacts.models import HIRING, OTHER, PEER, RECRUITER
from jobengine.drive_client import Drive, DriveError
from jobengine.gmail_client import Gmail, GmailError
from jobengine.llm import LLMClient
from jobengine.mail.compose import (
    ComposeError,
    Mail,
    Signature,
    check_attachment,
    compose,
    signature,
)
from jobengine.mail.fill import (
    FillError,
    MailJob,
    choose_template,
    clean_role,
    fill,
    generic_greeting,
    job_from_values,
    pick_detail,
    placeholder_values,
)
from jobengine.mail.templates import Template, TemplateError
from jobengine.notion_repo import ContactsRepo, JobsRepo, ResumeLogRepo
from jobengine.resume.builder import hex_id, notion_id, specific_details
from jobengine.safety import gmail_write_allowed, route_recipients
from jobengine.settings import ROOT_DIR, Settings

log = logging.getLogger("jobengine.mail")

ORDER = (HIRING, RECRUITER, PEER, OTHER)
BLOCKED_STATUSES = ("Bounced", "Do not contact")
DRAFTABLE_STATUSES = ("Unverified", "Verified")
NO_RESUME = "No approved resume for this job"
DRAFTED_RE = re.compile(r"drafted for (.+?) (\d{4}-\d{2}-\d{2})")
DEFAULT_COOLDOWN_DAYS = 30
DEFAULT_MAX_KB = 250
DRY_PREFIX = "DRY RUN: "


@dataclass
class MailDeps:
    s: Settings
    config: Callable[[], ConfigStore]
    jobs: JobsRepo | None
    contacts: ContactsRepo | None
    resume_log: ResumeLogRepo | None
    templates: Callable[[], dict[str, Template]]
    gmail: Callable[[], Gmail]
    drive: Callable[[], Drive]
    llm: Callable[[ConfigStore], LLMClient | None]
    today: Callable[[], date] = date.today
    write: bool = True  # False: --no-write, render only
    root: Path = ROOT_DIR  # where "DRY RUN: out/..." resume paths are resolved


@dataclass
class DraftLine:
    page_id: str
    name: str
    email: str
    type: str
    template: str = ""
    note: str = ""
    draft_id: str = ""
    thread_id: str = ""


@dataclass
class DraftsResult:
    job_id: str
    company: str = ""
    role: str = ""
    status: str = "done"  # done | failed
    message: str = ""
    drafted: list[DraftLine] = field(default_factory=list)  # created, or would be in DRY_RUN
    skipped: list[str] = field(default_factory=list)
    mails: list[Mail] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "done"


# ---------------------------------------------------------------- helpers


def is_generic(values: dict[str, Any], mailboxes: tuple[str, ...] | list[str]) -> bool:
    email = (values.get("Email") or "").strip().casefold()
    return values.get("Type") == OTHER and email.split("@", 1)[0] in {m.casefold()
                                                                     for m in mailboxes}


def last_mailed(values: dict[str, Any]) -> tuple[date | None, str | None]:
    """(date, role) of the latest "drafted for <Role> <date>" note, or Last contacted."""
    best: tuple[date | None, str | None] = (values.get("Last contacted"), None)
    for role, day in DRAFTED_RE.findall(values.get("Notes") or ""):
        try:
            when = date.fromisoformat(day)
        except ValueError:
            continue
        if best[0] is None or when >= best[0]:
            best = (when, role)
    return best


def drafted_for_role(values: dict[str, Any], role: str) -> bool:
    return any(r == role for r, _ in DRAFTED_RE.findall(values.get("Notes") or ""))


def _job_contacts(deps: MailDeps, job_id: str, values: dict[str, Any],
                  ids: list[str] | None) -> list[tuple[str, dict[str, Any]]]:
    """Contacts rows for the job: the given ids, else the job's Contacts relation and every
    row whose Related jobs has the job."""
    if deps.contacts is None:
        return []
    rows = deps.contacts.all_rows()
    wanted = {hex_id(i) for i in (ids or [])}
    if not wanted:
        wanted = {hex_id(i) for i in values.get("Contacts") or []}
        wanted |= {hex_id(pid) for pid, v in rows
                   if hex_id(job_id) in {hex_id(j) for j in v.get("Related jobs") or []}}
    picked = [(pid, v) for pid, v in rows if hex_id(pid) in wanted]
    return sorted(picked, key=lambda r: ORDER.index(r[1].get("Type"))
                  if r[1].get("Type") in ORDER else len(ORDER))


def approved_resume(deps: MailDeps, job_id: str) -> dict[str, Any] | None:
    """The newest approved Resume Log row for the job."""
    if deps.resume_log is None:
        return None
    rows = [v for _, v in deps.resume_log.rows_for_job(job_id) if v.get("Approved")]
    return max(rows, key=lambda v: int(v.get("Revision") or 0)) if rows else None


def resume_name(row: dict[str, Any]) -> str:
    """The Module 04 filename (Resume Log `Resume name` without the revision)."""
    name = (row.get("Resume name") or "resume.pdf").rsplit(" r", 1)[0]
    return name if name.endswith(".pdf") else f"{name}.pdf"


def resume_pdf(deps: MailDeps, row: dict[str, Any]) -> bytes:
    """The approved PDF: from out/ when it was saved in DRY_RUN, else from Drive."""
    file_value = (row.get("File") or "").strip()
    if not file_value:
        raise DriveError("The approved resume has no File. Approve it again.")
    if file_value.startswith(DRY_PREFIX):
        path = deps.root / file_value.removeprefix(DRY_PREFIX).strip()
        if not path.is_file():
            raise DriveError(f"The approved resume is not in out/ any more ({path.name}). "
                             "Tap Rebuild and approve it again.")
        return path.read_bytes()
    return deps.drive().download(file_value)


def env_where(s: Settings) -> str:
    account = s.gmail_sender.split("@", 1)[0]
    if s.app_env == "prod":
        return account
    return f"{account} in {s.env_label.strip('[]') or s.app_env.upper()}"


# ---------------------------------------------------------------- the run


def create_drafts(deps: MailDeps, job_id: str, contact_ids: list[str] | None = None,
                  key: str | None = None) -> DraftsResult:
    job_id = notion_id(job_id)
    dry = not gmail_write_allowed(deps.s) or not deps.write
    result = DraftsResult(job_id=job_id, dry_run=dry)

    def failed(message: str) -> DraftsResult:
        result.status, result.message = "failed", message
        return result

    values = deps.jobs.get_values(job_id) if deps.jobs else None
    if not values:
        return failed(f"No Job Opportunities row {job_id}.")
    job = job_from_values(job_id, values, specific_details(deps.jobs.read_body(job_id)))
    result.company, result.role = job.company, clean_role(job.role)
    config = deps.config()
    row = approved_resume(deps, job_id)
    if row is None:
        return failed(f"{NO_RESUME}: {job.company}, {job.role}.")
    try:
        templates = deps.templates()
        sig = signature(config)
        attachment = None
        if (config.get("mail.attach_resume") or "yes").casefold() != "no":
            attachment = (resume_name(row), resume_pdf(deps, row))
            check_attachment(attachment[1], config.get_int("mail.max_attachment_kb",
                                                           DEFAULT_MAX_KB))
        gmail = None if dry else deps.gmail()  # checks the token scope before any API call
    except (TemplateError, ComposeError, DriveError, GmailError) as exc:
        return failed(str(exc))

    rows = _job_contacts(deps, job_id, values, contact_ids)
    if not rows:
        return failed(f"No contacts for {job.company}, {job.role}. Run /contacts first.")
    today = deps.today()
    cooldown = config.get_int("mail.same_person_cooldown_days", DEFAULT_COOLDOWN_DAYS)
    max_kb = config.get_int("mail.max_attachment_kb", DEFAULT_MAX_KB)
    mailboxes = deps.s.contacts.get("generic_mailboxes") or GENERIC_MAILBOXES
    greeting = generic_greeting(config)
    llm = deps.llm(config) if job.details and len(job.details) > 1 else None
    detail = pick_detail(job.details, job, llm, key=key or job_id)
    emails_done: set[str] = set()

    for page_id, contact in rows:
        line = DraftLine(page_id=page_id, name=contact.get("Name") or "",
                         email=(contact.get("Email") or "").strip(), type=contact.get("Type") or "")
        reason = _skip_reason(contact, line, job, today, cooldown, mailboxes, greeting,
                              emails_done)
        if reason:
            result.skipped.append(f"{line.name or line.email} ({reason})")
            continue
        generic = is_generic(contact, mailboxes)
        choice = choose_template(templates, line.type, bool(detail), generic=generic)
        if choice.template is None:
            result.skipped.append(f"{line.name or line.email} ({choice.skipped})")
            continue
        line.template, line.note = choice.template.key, choice.note
        try:
            mail = _render(deps.s, job, config, choice.template, line, detail, greeting if generic
                           else None, sig, attachment, max_kb)
        except (FillError, ComposeError) as exc:
            result.skipped.append(f"{line.name or line.email} (aborted: {exc})")
            continue
        emails_done.add(line.email.casefold())
        result.mails.append(mail)
        if dry:
            log.warning("DRY RUN: would create draft to %s (%s)", mail.to, line.email)
        else:
            assert gmail is not None
            draft = gmail.create_draft(mail.raw())
            line.draft_id, line.thread_id = draft.draft_id, draft.thread_id
            _write_contact(deps, page_id, contact, line, job, today)
        result.drafted.append(line)

    if result.drafted and not dry and deps.jobs is not None:
        deps.jobs.update(job_id, {"Last activity date": today})
    result.message = summary(deps, result, config)
    return result


def _skip_reason(contact: dict[str, Any], line: DraftLine, job: MailJob, today: date,
                 cooldown: int, mailboxes: tuple[str, ...] | list[str], greeting: str | None,
                 emails_done: set[str]) -> str | None:
    status = contact.get("Status") or ""
    if status in BLOCKED_STATUSES:
        return status
    if not line.email or "@" not in line.email:
        return "no email"
    if line.email.casefold() in emails_done:
        return "same email as another contact"
    if contact.get("Gmail draft ID") or contact.get("Gmail thread ID"):
        if drafted_for_role(contact, clean_role(job.role)) or not DRAFTED_RE.search(
                contact.get("Notes") or ""):
            return "already drafted"
    when, role = last_mailed(contact)
    if when is not None and today - when < timedelta(days=cooldown):
        what = f" for {role}" if role else ""
        return f"mailed {(today - when).days} days ago{what}, cooldown {cooldown} days"
    if line.type == OTHER:
        if not is_generic(contact, mailboxes):
            return "Type Other"
        if not greeting:
            return "generic mailbox, no Config mail.generic_greeting_name"
    return None


def _render(s: Settings, job: MailJob, config: ConfigStore, template: Template,
            line: DraftLine, detail: str | None, greeting: str | None, sig: Signature,
            attachment: tuple[str, bytes] | None, max_kb: int) -> Mail:
    values = placeholder_values(job, line.name, config,
                                detail if template.key == "hiring" else None, greeting)
    filled = fill(template, values)
    to, _cc, subject = route_recipients([line.email], [], filled.subject, s)
    return compose(to[0], subject, filled.body, sig, attachment, max_kb=max_kb)


def _write_contact(deps: MailDeps, page_id: str, contact: dict[str, Any], line: DraftLine,
                   job: MailJob, today: date) -> None:
    if deps.contacts is None:
        log.warning("DRY RUN: would write to contacts")
        return
    note = f"drafted for {clean_role(job.role)} {today.isoformat()}"
    notes = (contact.get("Notes") or "").strip()
    props: dict[str, Any] = {"Gmail draft ID": line.draft_id, "Gmail thread ID": line.thread_id,
                             "Notes": f"{notes}\n{note}" if notes else note}
    if (contact.get("Status") or "") in DRAFTABLE_STATUSES:
        props["Status"] = "Drafted"
    deps.contacts.update(page_id, props)


# ---------------------------------------------------------------- summary


def _group_lines(result: DraftsResult) -> list[str]:
    lines = []
    for kind in ORDER:
        group = [d for d in result.drafted if d.type == kind]
        by_template: dict[tuple[str, str], list[str]] = {}
        for d in group:
            by_template.setdefault((d.template, d.note), []).append(d.name or d.email)
        for (template, note), names in by_template.items():
            extra = f", {note}" if note else ""
            label = "Mailbox" if kind == OTHER else kind
            lines.append(f"{label}: {', '.join(names)} ({template} template{extra})")
    return lines


def summary(deps: MailDeps, result: DraftsResult, config: ConfigStore) -> str:
    lines = [f"Drafts for {result.company}, {result.role}", *_group_lines(result)]
    n = len(result.drafted)
    if result.dry_run:
        lines.append(f"DRY RUN: {n} draft{'s' if n != 1 else ''} not created.")
        if n:
            lines.append("Would write to Contacts: Gmail draft ID, Gmail thread ID, Status "
                         "Drafted, Notes; and Last activity date on the job.")
    elif n:
        lines.append(f"{n} draft{'s' if n != 1 else ''} created in Gmail ({env_where(deps.s)}). "
                     "Review and send them from Gmail > Drafts.")
    else:
        lines.append("No drafts created.")
    lines.append(f"Skipped: {'; '.join(result.skipped) if result.skipped else 'none'}")
    cap = config.get_int("mail.daily_send_cap")
    if cap:
        lines.append(f"Reminder: soft cap is {cap} mails a day.")
        waiting = sum(1 for _, v in (deps.contacts.all_rows() if deps.contacts else [])
                      if v.get("Status") == "Drafted")
        if waiting > cap:
            lines.append(f"You have {waiting} unsent drafts; send at most {cap} today.")
    if result.dry_run and result.mails:
        lines += ["", "First mail:", result.mails[0].preview()]
    return "\n".join(lines)


# ---------------------------------------------------------------- deps

FIXTURES = ROOT_DIR / "fixtures" / "mail"


def fake_pdf(label: str) -> bytes:
    """A tiny invented PDF for fake runs (real resume content never enters the repo)."""
    return (f"%PDF-1.4\n% invented test resume for Alex Example, {label}\n%%EOF\n").encode()


def seed_resume_log(resume_log: Any, drive: Any, s: Settings, jobs: JobsRepo,
                    root: Path, out_dir: Path) -> None:
    """Revision 1 (not approved) and revision 2 (approved) for every fixture job. The approved
    PDF is in the fake Drive, or in out/ when DRY_RUN is on (as Module 04 saves it)."""
    from jobengine.resume.render import filename

    for page_id, values in jobs.query_rows():
        name = filename(ConfigStore.fake().get("resume.filename"), values.get("Company") or "")
        resume_log.create({"Resume name": f"{name} r1", "Job": [page_id], "Revision": 1,
                           "Approved": False}, [])
        pdf = fake_pdf(f"{page_id} r2")
        if s.dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / name).write_bytes(pdf)
            file_value = DRY_PREFIX + (out_dir / name).relative_to(root).as_posix()
        else:
            file_value = drive.upload_pdf(name, pdf)
        resume_log.create({"Resume name": f"{name} r2", "Job": [page_id], "Revision": 2,
                           "Approved": True, "File": file_value}, [])


def fake_deps(
    s: Settings,
    jobs: JobsRepo | None = None,
    contacts: ContactsRepo | None = None,
    resume_log: ResumeLogRepo | None = None,
    drive: Drive | None = None,
    gmail: Gmail | None = None,
    write: bool = True,
    root: Path = ROOT_DIR,
) -> MailDeps:
    """Fixtures for Notion, the templates, the LLM, Drive and Gmail. Without a Resume Log,
    one is seeded with an approved revision per fixture job."""
    from jobengine.drive_client import FakeDrive
    from jobengine.gmail_client import FakeGmail
    from jobengine.llm import FakeLLM
    from jobengine.mail.templates import fake_templates
    from jobengine.notion_repo import FakeContactsRepo, FakeJobsRepo, FakeResumeLogRepo

    jobs = jobs or FakeJobsRepo.from_fixture(FIXTURES / "job_opportunities_seed.json")
    contacts = contacts or FakeContactsRepo.from_fixture(FIXTURES / "contacts_seed.json")
    the_drive = drive or FakeDrive()
    the_gmail = gmail or FakeGmail()
    if resume_log is None:
        resume_log = FakeResumeLogRepo()
        seed_resume_log(resume_log, the_drive, s, jobs, root, root / "out" / "fake" / "mail")
    return MailDeps(
        s=s, config=ConfigStore.fake, jobs=jobs, contacts=contacts, resume_log=resume_log,
        templates=fake_templates, gmail=lambda: the_gmail, drive=lambda: the_drive,
        llm=lambda config: FakeLLM(default={"index": 1}), write=write, root=root,
    )


def real_deps(s: Settings, write: bool = True) -> MailDeps:
    from jobengine.drive_client import GoogleDrive
    from jobengine.gmail_client import GmailClient
    from jobengine.llm import AnthropicLLM
    from jobengine.mail.templates import load_templates
    from jobengine.notion_repo import (
        NotionClient,
        contacts_repo_for,
        jobs_repo_for,
        resume_log_for,
    )
    from jobengine.safety import llm_allowed

    if not s.notion_token:
        raise TemplateError("mail drafter cannot run: NOTION_TOKEN missing")
    client = NotionClient(s.notion_token, s)
    page = s.notion_pages.get("cold_mail_templates", "")
    return MailDeps(
        s=s, config=lambda: ConfigStore.load(client, s), jobs=jobs_repo_for(s, client),
        contacts=contacts_repo_for(s, client), resume_log=resume_log_for(s, client),
        templates=lambda: load_templates(client, page),
        gmail=lambda: GmailClient.from_settings(s), drive=lambda: GoogleDrive.from_settings(s),
        llm=lambda config: AnthropicLLM(s, config) if llm_allowed(s) else None, write=write,
    )
