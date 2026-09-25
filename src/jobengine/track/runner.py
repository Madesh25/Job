"""The daily tracking run (spec section 2): sent drafts, replies, application confirmations,
bounces, follow-ups, ghosting and the retention list, then one Telegram report.

Gmail is only read, labelled (never in DRY_RUN) and given drafts; nothing is sent. Every
Notion write goes through the repos from notion_write_target. The marker
bot_state["tracking.last_success"] moves only after a run without errors.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from jobengine.bot_state import BotState
from jobengine.config_store import ConfigStore
from jobengine.gmail_client import TrackGmail
from jobengine.llm import LLMClient
from jobengine.mail.compose import ComposeError
from jobengine.mail.fill import FillError, job_from_values
from jobengine.mail.templates import Template
from jobengine.notion_repo import ContactsRepo, JobsRepo
from jobengine.reference import Reference
from jobengine.resume.builder import hex_id
from jobengine.safety import gmail_write_allowed
from jobengine.settings import Settings
from jobengine.sweep.normalize import canon_company
from jobengine.track import bounces, followup, linking
from jobengine.track.classify import DEFAULT_OPT_OUT, classify, is_auto_reply
from jobengine.track.models import (
    AUTO_REPLY,
    FOLLOWUP_SENT,
    INTERVIEW,
    OPT_OUT,
    Classification,
    Message,
)
from jobengine.track.status import JOB_TERMINAL, advance_contact, effect

log = logging.getLogger("jobengine.track")

MARKER = "tracking.last_success"
ASKED = "tracking.asked"
MAX_ASKED = 300
PREVIEW_CHARS = 300
DEFAULT_ATS = ("greenhouse.io", "lever.co", "smartrecruiters.com", "myworkday.com",
               "workday.com", "teamtailor.com", "recruitee.com", "personio.de",
               "successfactors.com", "icims.com")
CLOSED_CONTACTS = ("Ghosted", "Bounced", "Do not contact")
# Buttons on a low-confidence card: (label, class). "positive" means Replied.
CHOICES = (("Screening", "screening"), ("Interview", "interview"), ("Rejected", "rejection"),
           ("Offer", "offer"), ("Replied", "positive"), ("Do not contact", "opt_out"),
           ("Ignore", "ignore"))
TOKEN_FAILED = "Gmail token for {account} failed: {reason}. Regenerate it (docs/gmail.md)."


@dataclass
class TrackDeps:
    s: Settings
    config: Callable[[], ConfigStore]
    jobs: JobsRepo | None
    contacts: ContactsRepo | None
    state: BotState
    gmail: Callable[[], TrackGmail]
    reference: Callable[[], Reference]
    templates: Callable[[], dict[str, Template]]
    llm: Callable[[ConfigStore], LLMClient | None]
    # "alerts" | "sender" -> None when the token refreshes, else the reason.
    token_check: Callable[[str], str | None]
    write: bool = True  # False: --no-write, report only


@dataclass
class Card:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DailyReport:
    day: date
    since: datetime
    tokens: dict[str, str | None] = field(default_factory=dict)
    sent: list[str] = field(default_factory=list)
    deleted_drafts: list[str] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    applications: list[str] = field(default_factory=list)
    bounced: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    followups_dry: list[str] = field(default_factory=list)
    followup_problems: list[str] = field(default_factory=list)
    ghosted_contacts: list[str] = field(default_factory=list)
    ghosted_jobs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    retention: Card | None = None
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and all(v is None for v in self.tokens.values())

    def text(self) -> str:
        lines = self.token_lines()
        lines.append(f"Daily check {self.day.isoformat()} (since "
                     f"{self.since.strftime('%Y-%m-%d %H:%M')})")
        if self.sent:
            lines.append(f"Sent: {len(self.sent)} mails detected ({', '.join(self.sent)})")
        if self.deleted_drafts:
            lines.append(f"Drafts deleted in Gmail: {', '.join(self.deleted_drafts)}")
        if self.replies:
            lines.append(f"Replies: {'; '.join(self.replies)}")
        if self.applications:
            lines.append(f"Applications: {'; '.join(self.applications)}")
        if self.bounced:
            lines.append(f"Bounced: {len(self.bounced)} ({', '.join(self.bounced)})")
        if self.followups:
            lines.append(f"Follow-ups drafted: {len(self.followups)} (send them from Gmail > "
                         f"Drafts): {', '.join(self.followups)}")
        if self.followups_dry:
            lines.append(f"DRY RUN: {len(self.followups_dry)} follow-ups not drafted "
                         f"({', '.join(self.followups_dry)})")
        if self.followup_problems:
            lines.append(f"Follow-ups not possible: {'; '.join(self.followup_problems)}")
        if self.ghosted_contacts or self.ghosted_jobs:
            c, j = len(self.ghosted_contacts), len(self.ghosted_jobs)
            lines.append(f"Ghosted: {c} contact{'' if c == 1 else 's'}, {j} "
                         f"job{'' if j == 1 else 's'}")
        asks = len(self.cards)
        if asks:
            lines.append(f"Needs your call: {asks} (see below)")
        lines.extend(self.notes)
        lines.extend(f"Error: {e}" for e in self.errors)
        lines.append("Tokens: " + ", ".join(
            f"{name} {'OK' if reason is None else 'FAILED'}"
            for name, reason in self.tokens.items()))
        return "\n".join(lines)

    def token_lines(self) -> list[str]:
        return [TOKEN_FAILED.format(account=name, reason=reason)
                for name, reason in self.tokens.items() if reason is not None]


# ---------------------------------------------------------------- helpers


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _append(notes: str | None, line: str) -> str:
    notes = (notes or "").strip()
    return f"{notes}\n{line}" if notes else line


class Run:
    """One daily run: in-memory copies of the rows plus guarded writes."""

    def __init__(self, deps: TrackDeps, now: datetime):
        self.deps = deps
        self.s = deps.s
        self.now = now
        self.today = now.date()
        self.config = deps.config()
        self.dry = not gmail_write_allowed(deps.s)
        self.contacts: dict[str, dict[str, Any]] = dict(
            deps.contacts.all_rows() if deps.contacts else [])
        self.jobs: dict[str, dict[str, Any]] = dict(deps.jobs.query_rows() if deps.jobs else [])
        self.mine = {a.casefold() for a in (deps.s.gmail_sender, deps.s.dev_sender,
                                            deps.s.prod_sender, deps.s.redirect_to) if a}
        asked = deps.state.get(ASKED) or {}
        self.asked: list[str] = list(asked.get("ids") or [])
        self._labels: dict[str, str] | None = None
        self._missing: set[str] = set()
        self._llm: LLMClient | None = None
        self._llm_ready = False
        self._gmail: TrackGmail | None = None
        t = deps.s.tracking
        self.min_conf = self._float("tracking.min_confidence", t.get("min_confidence", 0.8))
        self.followup_days = self._int("followup.days", t.get("followup_days", 7))
        self.ghosted_days = self._int("ghosted.days", t.get("ghosted_days", 14))
        self.retention_months = self._int("contacts.retention_months",
                                          t.get("retention_months", 12))
        self.phrases = t.get("opt_out_phrases") or list(DEFAULT_OPT_OUT)
        self.ats = list(t.get("ats_sender_domains") or DEFAULT_ATS)

    # ------------------------------------------------------------ config

    def _int(self, key: str, default: Any) -> int:
        return self.config.get_int(key, int(default)) or int(default)

    def _float(self, key: str, default: Any) -> float:
        try:
            return float(self.config.get(key) or default)
        except ValueError:
            return float(default)

    # ------------------------------------------------------------ services

    @property
    def gmail(self) -> TrackGmail:
        if self._gmail is None:
            self._gmail = self.deps.gmail()
        return self._gmail

    def llm(self) -> LLMClient | None:
        if not self._llm_ready:
            self._llm, self._llm_ready = self.deps.llm(self.config), True
        return self._llm

    # ------------------------------------------------------------ writes

    def set_contact(self, page_id: str, props: dict[str, Any]) -> None:
        self.contacts[page_id].update(props)
        if self.deps.write and self.deps.contacts is not None:
            self.deps.contacts.update(page_id, props)

    def set_job(self, page_id: str, props: dict[str, Any]) -> None:
        self.jobs.setdefault(page_id, {}).update(props)
        if self.deps.write and self.deps.jobs is not None:
            self.deps.jobs.update(page_id, props)

    def label(self, thread_id: str, key: str, report: DailyReport) -> None:
        """Apply the configured label; never create one; nothing in DRY_RUN."""
        name = (self.s.tracking.get("labels") or {}).get(key)
        if not name:
            return
        if self.dry or not self.deps.write:
            log.info("DRY RUN: would label thread %s with %s", thread_id, name)
            return
        if self._labels is None:
            self._labels = self.gmail.labels()
        label_id = self._labels.get(name)
        if label_id is None:
            if name not in self._missing:
                self._missing.add(name)
                report.notes.append(f"Gmail label {name} not found (labels are never "
                                    "created): make it in Gmail.")
            return
        self.gmail.label_thread(thread_id, label_id)

    def ask(self, key: str) -> bool:
        """True the first time `key` is asked about (cards are not repeated every day)."""
        if key in self.asked:
            return False
        self.asked.append(key)
        return True

    # ------------------------------------------------------------ lookups

    def related_jobs(self, contact: dict[str, Any]) -> list[str]:
        ids = {hex_id(j) for j in contact.get("Related jobs") or []}
        return [pid for pid in self.jobs if hex_id(pid) in ids]

    def job_contacts(self, job_id: str) -> list[dict[str, Any]]:
        linked = {hex_id(c) for c in self.jobs.get(job_id, {}).get("Contacts") or []}
        return [v for pid, v in self.contacts.items() if hex_id(pid) in linked
                or hex_id(job_id) in {hex_id(j) for j in v.get("Related jobs") or []}]

    def company_of(self, contact: dict[str, Any]) -> str:
        jobs = self.related_jobs(contact)
        return (self.jobs[jobs[0]].get("Company") if jobs else None) or \
            contact.get("Company") or ""

    def replies_in(self, thread: list[Message]) -> list[Message]:
        """Messages after your last sent message that are not yours, not automatic and not
        bounces."""
        mine_idx = max((i for i, m in enumerate(thread) if m.sent or m.sender in self.mine),
                       default=-1)
        return [m for m in thread[mine_idx + 1:]
                if m.sender not in self.mine and not is_auto_reply(m)
                and not bounces.is_bounce(m)]


# ---------------------------------------------------------------- steps


def detect_sent(run: Run, report: DailyReport) -> None:
    """Step 2: drafts you sent or deleted, first mails and follow-ups."""
    for pid, c in list(run.contacts.items()):
        name = c.get("Name") or c.get("Email") or pid
        draft = c.get("Gmail draft ID")
        if c.get("Status") == "Drafted" and draft and not run.gmail.draft_exists(draft):
            thread = run.gmail.thread(c.get("Gmail thread ID") or "") \
                if c.get("Gmail thread ID") else []
            sent = [m for m in thread if m.sent]
            if sent:
                when = sent[-1].when.date()
                run.set_contact(pid, {"Status": "Contacted", "Last contacted": when,
                                      "Gmail draft ID": ""})
                for job_id in run.related_jobs(c):
                    run.set_job(job_id, {"Last activity date": when})
                report.sent.append(name)
            else:
                back = "Unverified" if "(was Unverified)" in (c.get("Notes") or "") \
                    else "Verified"
                run.set_contact(pid, {"Status": back, "Gmail draft ID": "",
                                      "Gmail thread ID": "",
                                      "Notes": _append(c.get("Notes"),
                                                       f"draft deleted {run.today}")})
                report.deleted_drafts.append(f"{name} (back to {back})")
        follow = c.get("Follow-up draft ID")
        if follow and c.get("Status") in ("Contacted", "Followed up") \
                and not run.gmail.draft_exists(follow):
            thread = run.gmail.thread(c.get("Gmail thread ID") or "")
            last = _day(c.get("Last contacted"))
            later = [m for m in thread if m.sent and (last is None or m.when.date() > last)]
            if later:
                when = later[-1].when.date()
                props: dict[str, Any] = {"Last contacted": when, "Follow-up draft ID": ""}
                new = advance_contact(c.get("Status"), "Followed up")
                if new:
                    props["Status"] = new
                run.set_contact(pid, props)
                for job_id in run.related_jobs(c):
                    fx = effect(FOLLOWUP_SENT, run.jobs[job_id].get("Status"), cold_mail=False)
                    job_props: dict[str, Any] = {"Last activity date": when}
                    if fx.job_status:
                        job_props["Status"] = fx.job_status
                    run.set_job(job_id, job_props)
                report.sent.append(f"{name} (follow-up)")
            else:
                run.set_contact(pid, {"Follow-up draft ID": "", "Notes": _append(
                    c.get("Notes"), f"follow-up draft deleted {run.today}")})
                report.deleted_drafts.append(f"{name} (follow-up)")


def _card(run: Run, message: Message, kind: str, target: str, name: str,
          cls: Classification) -> Card:
    text = strip_preview(message.text or message.snippet)
    guess = f"{cls.kind} {cls.confidence:.2f}" if cls.confidence else "unclear"
    return Card(
        text=(f"Reply from {message.sender} ({name}), my guess: {guess}\n"
              f"Subject: {message.subject}\n{text}"),
        buttons=[(label, f"rc:{kind}:{hex_id(target)}:{value}") for label, value in CHOICES],
    )


def strip_preview(text: str) -> str:
    from jobengine.track.classify import strip_quoted

    return strip_quoted(text, PREVIEW_CHARS)


def apply_contact_class(run: Run, pid: str, kind: str, report: DailyReport | None) -> str:
    """Section 3 for a cold-mail reply. Returns the report line."""
    c = run.contacts[pid]
    name = c.get("Name") or c.get("Email") or pid
    target = "Do not contact" if kind == OPT_OUT else "Replied"
    props: dict[str, Any] = {}
    new = advance_contact(c.get("Status"), target)
    if new:
        props["Status"] = new
    if c.get("Follow-up draft ID"):
        props["Follow-up draft ID"] = ""
        if report is not None:
            report.notes.append(f"A follow-up draft for {name} is no longer needed; delete it "
                                "in Gmail.")
    if props:
        run.set_contact(pid, props)
    outcome = new or c.get("Status") or target
    for job_id in run.related_jobs(c):
        fx = effect(kind, run.jobs[job_id].get("Status"), c.get("Status"))
        if fx.touch:
            job_props: dict[str, Any] = {"Last activity date": run.today}
            if fx.job_status:
                job_props["Status"] = fx.job_status
                outcome = fx.job_status
            run.set_job(job_id, job_props)
    return f"{name} ({run.company_of(c)}) -> {outcome}"


def cold_replies(run: Run, report: DailyReport) -> None:
    """Step 3: replies in the threads of your cold mails."""
    for pid, c in list(run.contacts.items()):
        if c.get("Status") not in ("Contacted", "Followed up") or not c.get("Gmail thread ID"):
            continue
        replies = run.replies_in(run.gmail.thread(c["Gmail thread ID"]))
        if not replies:
            continue
        message = replies[-1]
        cls = classify(message, run.llm(), phrases=run.phrases)
        if cls.kind == AUTO_REPLY:
            continue
        if cls.confidence < run.min_conf:
            if run.ask(message.id):
                report.cards.append(_card(run, message, "c", pid, c.get("Name") or "", cls))
            continue
        report.replies.append(apply_contact_class(run, pid, cls.kind, report))
        run.label(c["Gmail thread ID"], "replies", report)


def apply_job_class(run: Run, job_id: str, kind: str) -> str | None:
    job = run.jobs[job_id]
    fx = effect(kind, job.get("Status"), cold_mail=False)
    if not fx.touch:
        return None
    props: dict[str, Any] = {"Last activity date": run.today}
    if fx.job_status:
        props["Status"] = fx.job_status
    run.set_job(job_id, props)
    return f"{job.get('Company')} -> {fx.job_status}" if fx.job_status else None


def process_job_thread(run: Run, job_id: str, since: datetime, report: DailyReport) -> None:
    """Step 4 for one job thread: new inbound messages, in order."""
    thread_id = run.jobs[job_id].get("Gmail thread ID") or ""
    inbound = [m for m in run.gmail.thread(thread_id)
               if m.when >= since and m.sender not in run.mine and not m.sent
               and not is_auto_reply(m) and not bounces.is_bounce(m)]
    for message in inbound:
        cls = classify(message, run.llm(), phrases=run.phrases)
        if cls.kind == AUTO_REPLY:
            continue
        if cls.confidence < run.min_conf:
            if run.ask(message.id):
                report.cards.append(_card(run, message, "j", job_id,
                                          run.jobs[job_id].get("Company") or "", cls))
            continue
        line = apply_job_class(run, job_id, cls.kind)
        if line:
            report.applications.append(line)
        run.label(thread_id, "applications", report)
        if cls.kind == INTERVIEW:
            run.label(thread_id, "interviews", report)


def job_threads(run: Run, since: datetime, report: DailyReport) -> None:
    for job_id, job in list(run.jobs.items()):
        if job.get("Gmail thread ID") and job.get("Status") not in JOB_TERMINAL:
            process_job_thread(run, job_id, since, report)


def company_domain(run: Run, company: str) -> str | None:
    target = run.deps.reference().company(company)
    if target and target.domain:
        return target.domain
    answer = run.deps.state.get(f"domain:{canon_company(company)}") or {}
    return answer.get("domain")


def link_applications(run: Run, report: DailyReport) -> None:
    """Step 5: find the confirmation thread of jobs you applied to."""
    for job_id, job in list(run.jobs.items()):
        if job.get("Status") != "Applied" or job.get("Gmail thread ID"):
            continue
        applied = _day(job.get("Applied date"))
        if applied is None:
            continue
        company = job.get("Company") or ""
        domain = company_domain(run, company)
        domains = ([domain] if domain else []) + run.ats
        found = linking.candidates(run.gmail.search(linking.query(domains, applied)), company,
                                   domains)
        if found.thread_id:
            run.set_job(job_id, {"Gmail thread ID": found.thread_id})
            report.applications.append(f"{company} linked")
            run.label(found.thread_id, "applications", report)
            since = datetime.combine(applied, datetime.min.time(), tzinfo=run.now.tzinfo)
            process_job_thread(run, job_id, since, report)
        elif len(found.candidates) > 1:
            for m in found.candidates:
                if not run.ask(f"link:{hex_id(job_id)}:{m.thread_id}"):
                    continue
                report.cards.append(Card(
                    text=(f"Is this the application thread for {company}, "
                          f"{job.get('Role')}?\nFrom: {m.sender}\nSubject: {m.subject}\n"
                          f"{strip_preview(m.snippet or m.text)}"),
                    buttons=[("Link", f"lk:{hex_id(job_id)}:{m.thread_id}"),
                             ("Not this", f"lk:{hex_id(job_id)}:-")]))


def find_bounces(run: Run, window: str, report: DailyReport) -> None:
    """Step 6."""
    by_email = {(c.get("Email") or "").casefold(): pid for pid, c in run.contacts.items()}
    for message in run.gmail.search(f"{window} from:(mailer-daemon OR postmaster)"):
        if not bounces.is_bounce(message):
            continue
        for address in bounces.failed_recipients(message):
            pid = by_email.get(address)
            if pid is None:
                continue
            c = run.contacts[pid]
            new = advance_contact(c.get("Status"), "Bounced")
            if new is None:
                continue
            run.set_contact(pid, {"Status": new, "Notes": _append(
                c.get("Notes"), f"bounced {message.when.date().isoformat()}")})
            report.bounced.append(address)


def draft_followups(run: Run, report: DailyReport) -> None:
    """Step 7: one follow-up per contact, ever, after `followup.days` of silence."""
    due = run.today - timedelta(days=run.followup_days)
    templates: dict[str, Template] | None = None
    for pid, c in list(run.contacts.items()):
        last = _day(c.get("Last contacted"))
        if c.get("Status") != "Contacted" or last is None or last > due:
            continue
        if c.get("Follow-up draft ID") or followup.NOTE in (c.get("Notes") or ""):
            continue
        thread_id = c.get("Gmail thread ID")
        if not thread_id:
            continue
        thread = run.gmail.thread(thread_id)
        if run.replies_in(thread):
            continue  # a reply waits for your call
        name = c.get("Name") or c.get("Email") or pid
        jobs = run.related_jobs(c)
        if not jobs:
            report.followup_problems.append(f"{name} (no related job)")
            continue
        if run.dry or not run.deps.write:
            log.warning("DRY RUN: would draft a follow-up for %s", c.get("Email"))
            report.followups_dry.append(name)
            continue
        templates = templates if templates is not None else run.deps.templates()
        job = job_from_values(jobs[0], run.jobs[jobs[0]], [])
        try:
            built = followup.build(c, job, thread, run.mine, templates, run.config, run.s)
        except (followup.FollowupError, FillError, ComposeError) as exc:
            report.followup_problems.append(f"{name} ({exc})")
            continue
        draft = run.gmail.create_draft(built.mail.raw(), thread_id=built.thread_id)
        run.set_contact(pid, {"Follow-up draft ID": draft.draft_id, "Notes": _append(
            c.get("Notes"), f"{followup.NOTE} {run.today}")})
        report.followups.append(name)


def mark_ghosted(run: Run, report: DailyReport) -> None:
    """Step 8: contacts silent `ghosted.days` after the follow-up, then jobs whose contacts
    are all closed."""
    cutoff = run.today - timedelta(days=run.ghosted_days)
    for pid, c in list(run.contacts.items()):
        last = _day(c.get("Last contacted"))
        if c.get("Status") == "Followed up" and last is not None and last <= cutoff:
            run.set_contact(pid, {"Status": "Ghosted"})
            report.ghosted_contacts.append(c.get("Name") or pid)
    job_cutoff = run.today - timedelta(days=run.followup_days + run.ghosted_days)
    for job_id, job in list(run.jobs.items()):
        applied = _day(job.get("Applied date"))
        if job.get("Status") not in ("Applied", "Followed up") or applied is None \
                or applied > job_cutoff:
            continue
        if all(c.get("Status") in CLOSED_CONTACTS for c in run.job_contacts(job_id)):
            run.set_job(job_id, {"Status": "Ghosted", "Last activity date": run.today})
            report.ghosted_jobs.append(job.get("Company") or job_id)


def months_ago(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    return date(year, month + 1, min(day.day, 28))


def retention_candidates(contacts: dict[str, dict[str, Any]], today: date,
                         months: int) -> list[str]:
    """Contacts that never replied, found more than `months` ago. Do not contact rows are
    kept (so they are never contacted again)."""
    cutoff = months_ago(today, months)
    return [pid for pid, c in contacts.items()
            if c.get("Status") not in ("Replied", "Do not contact")
            and _day(c.get("Date found")) is not None and _day(c.get("Date found")) < cutoff]


def list_retention(run: Run, report: DailyReport) -> None:
    """Step 9: only a list and a button; nothing is deleted without your tap."""
    old = retention_candidates(run.contacts, run.today, run.retention_months)
    if old:
        report.retention = Card(
            text=(f"{len(old)} contacts never replied and were found more than "
                  f"{run.retention_months} months ago."),
            buttons=[(f"Delete {len(old)} old contacts",
                      f"rd:{run.today.strftime('%Y%m%d')}")])


# ---------------------------------------------------------------- the run


def window_start(state: BotState, now: datetime, s: Settings) -> datetime:
    t = s.tracking
    last = (state.get(MARKER) or {}).get("at")
    if last:
        try:
            return datetime.fromisoformat(last) - timedelta(days=int(t.get("overlap_days", 1)))
        except ValueError:
            log.warning("bot_state %s is not a date: %s", MARKER, last)
    return now - timedelta(days=int(t.get("first_run_days", 14)))


def run_daily(deps: TrackDeps, now: datetime) -> DailyReport:
    since = window_start(deps.state, now, deps.s)
    report = DailyReport(day=now.date(), since=since.astimezone(now.tzinfo),
                         dry_run=not gmail_write_allowed(deps.s))
    for name in ("alerts", "sender"):
        report.tokens[name] = deps.token_check(name)
    run = Run(deps, now)
    window = f"after:{int(since.timestamp())} -in:chats"
    steps: list[tuple[str, Callable[[], None]]] = []
    if report.tokens.get("sender") is None:
        steps = [
            ("sent detection", lambda: detect_sent(run, report)),
            ("replies", lambda: cold_replies(run, report)),
            ("job threads", lambda: job_threads(run, since, report)),
            ("application linking", lambda: link_applications(run, report)),
            ("bounces", lambda: find_bounces(run, window, report)),
            ("follow-ups", lambda: draft_followups(run, report)),
            ("ghosted", lambda: mark_ghosted(run, report)),
        ]
    steps.append(("retention", lambda: list_retention(run, report)))
    for name, step in steps:
        try:
            step()
        except Exception as exc:  # one failed step is reported; the marker does not move
            log.exception("daily check step %s failed", name)
            report.errors.append(f"{name} failed: {exc}")
    if deps.write:
        deps.state.set(ASKED, {"ids": run.asked[-MAX_ASKED:]})
        if report.ok:
            deps.state.set(MARKER, {"at": now.isoformat()})
    return report


# ---------------------------------------------------------------- taps


def _find(rows: dict[str, dict[str, Any]], hex_value: str) -> str | None:
    return next((pid for pid in rows if hex_id(pid) == hex_value.replace("-", "")), None)


def apply_choice(deps: TrackDeps, now: datetime, data: str) -> str:
    """rc:<c|j>:<page hex>:<class>: your answer to a low-confidence card."""
    _, kind, target, cls = (data.split(":") + ["", "", "", ""])[:4]
    if cls == "ignore":
        return "Ignored."
    run = Run(deps, now)
    if kind == "c":
        pid = _find(run.contacts, target)
        if pid is None:
            return "That contact is no longer in Contacts."
        line = apply_contact_class(run, pid, cls, None)
        thread = run.contacts[pid].get("Gmail thread ID")
        report = DailyReport(day=now.date(), since=now)
        if thread:
            run.label(thread, "replies", report)
        return "\n".join([f"Applied: {line}", *report.notes])
    job_id = _find(run.jobs, target)
    if job_id is None:
        return "That job is no longer in Job Opportunities."
    line = apply_job_class(run, job_id, cls)
    return f"Applied: {line}" if line else "No change (the status only moves forward)."


def link_choice(deps: TrackDeps, now: datetime, data: str) -> str:
    """lk:<job hex>:<thread id>, or lk:<job hex>:- for Not this."""
    _, target, thread_id = (data.split(":") + ["", "", ""])[:3]
    if thread_id in ("", "-"):
        return "OK, not linked."
    run = Run(deps, now)
    job_id = _find(run.jobs, target)
    if job_id is None:
        return "That job is no longer in Job Opportunities."
    if run.jobs[job_id].get("Gmail thread ID"):
        return "That job already has a Gmail thread."
    run.set_job(job_id, {"Gmail thread ID": thread_id})
    report = DailyReport(day=now.date(), since=now)
    run.label(thread_id, "applications", report)
    applied = _day(run.jobs[job_id].get("Applied date")) or now.date()
    process_job_thread(run, job_id, datetime.combine(applied, datetime.min.time(),
                                                     tzinfo=now.tzinfo), report)
    return "\n".join([f"Linked {run.jobs[job_id].get('Company')}.", *report.applications,
                      *report.notes])


def retention_choice(deps: TrackDeps, now: datetime, data: str) -> str:
    """rd:<yyyymmdd>: move the old contacts to the Notion trash, prod only."""
    run = Run(deps, now)
    old = retention_candidates(run.contacts, now.date(), run.retention_months)
    if deps.s.app_env != "prod":
        return f"DRY RUN: would delete {len(old)} old contacts (only prod deletes)."
    if deps.contacts is None or not deps.write:
        return "DRY RUN: no Contacts target here, nothing deleted."
    for pid in old:
        deps.contacts.archive(pid)
    return f"Deleted {len(old)} old contacts (moved to the Notion trash)."


# ---------------------------------------------------------------- deps


def fake_deps(s: Settings, write: bool = True, jobs: JobsRepo | None = None,
              contacts: ContactsRepo | None = None, state: BotState | None = None,
              gmail: TrackGmail | None = None) -> TrackDeps:
    """Fixture jobs, contacts and Gmail threads (fixtures/track), FakeLLM answers, all
    tokens healthy. Open drafts: d-jan and f-rita. Existing labels: Replies and
    Applications (Interviews is missing on purpose)."""
    from jobengine.bot_state import FakeBotState
    from jobengine.gmail_client import FakeGmail
    from jobengine.llm import FakeLLM
    from jobengine.mail.templates import fake_templates
    from jobengine.notion_repo import FakeContactsRepo, FakeJobsRepo
    from jobengine.track.fakes import FIXTURES, load_threads

    labels = s.tracking.get("labels") or {}
    the_gmail = gmail or FakeGmail(
        threads=load_threads(), open_drafts={"d-jan", "f-rita"},
        label_ids={name: f"Label_{i}" for i, (key, name) in enumerate(sorted(labels.items()))
                   if key != "interviews"})
    return TrackDeps(
        s=s, config=ConfigStore.fake,
        jobs=jobs or FakeJobsRepo.from_fixture(FIXTURES / "jobs_seed.json"),
        contacts=contacts or FakeContactsRepo.from_fixture(FIXTURES / "contacts_seed.json"),
        state=state or FakeBotState(), gmail=lambda: the_gmail, reference=Reference.fake,
        templates=lambda: fake_templates(FIXTURES / "cold_mail_templates_page.json"),
        llm=lambda config: FakeLLM(), token_check=lambda name: None, write=write,
    )


def real_deps(s: Settings, state: BotState, write: bool = True) -> TrackDeps:
    from jobengine.gmail_client import GmailClient, refresh_error
    from jobengine.llm import AnthropicLLM
    from jobengine.mail.templates import load_templates
    from jobengine.notion_repo import NotionClient, contacts_repo_for, jobs_repo_for
    from jobengine.safety import llm_allowed

    if not s.notion_token:
        raise ValueError("daily check cannot run: NOTION_TOKEN missing")
    client = NotionClient(s.notion_token, s)
    tokens = {"alerts": s.gmail_alerts_token_json, "sender": s.gmail_sender_token_json}
    page = s.notion_pages.get("cold_mail_templates", "")
    return TrackDeps(
        s=s, config=lambda: ConfigStore.load(client, s), jobs=jobs_repo_for(s, client),
        contacts=contacts_repo_for(s, client), state=state,
        gmail=lambda: GmailClient.from_settings(s), reference=lambda: Reference.load(client, s),
        templates=lambda: load_templates(client, page),
        llm=lambda config: AnthropicLLM(s, config) if llm_allowed(s) else None,
        token_check=lambda name: refresh_error(tokens[name]), write=write,
    )
