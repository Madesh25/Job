"""What the Telegram bot does with screened jobs (spec section 9): /pending cards and their
buttons, /jd capture and /done, /screen, and screening after /fetch.

The bot only turns updates into calls here and sends the returned replies. Nothing in this
module talks to Telegram.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from jobengine import http
from jobengine.bot_state import BotState, FakeBotState
from jobengine.contacts import finder as contact_finder
from jobengine.contacts.finder import ContactDeps
from jobengine.llm import LLMError
from jobengine.mail import drafter as mail_drafter
from jobengine.mail import sender as mail_sender
from jobengine.mail import timing as mail_timing
from jobengine.mail.drafter import MailDeps
from jobengine.notion_repo import FakeJobsRepo, JobsRepo
from jobengine.reference import Reference
from jobengine.resume import builder as resume_builder
from jobengine.resume.builder import BuildOutcome, ResumeDeps
from jobengine.screen import autopilot, batch, jd_capture
from jobengine.screen.jd_capture import Capture, Captures
from jobengine.screen.models import JobRow, ScreenSummary
from jobengine.screen.runner import (
    SECTION_PREFIX,
    ScreenDeps,
    ScreenError,
    description,
    find_row,
    job_row,
    pending_rows,
    screen_one,
    screen_pending,
    waiting_for_jd,
)
from jobengine.screen.tiering import rank_key
from jobengine.settings import ROOT_DIR, Settings
from jobengine.strategy import runner as strategy_runner
from jobengine.strategy.runner import StrategyDeps
from jobengine.sweep.normalize import dedupe_key
from jobengine.sweep.rank import Ranker
from jobengine.track import commands as track_commands
from jobengine.track import runner as track_runner
from jobengine.track.digest import run_weekly_digest
from jobengine.track.runner import DailyReport, TrackDeps

log = logging.getLogger("jobengine.screen.desk")

APPROVED_TEXT = "Approved: {job}."
NO_RESUME = "Resume building is not available in this bot."
REBUILD_KEY = "resume.rebuild_ask"  # bot_state: {"log": <Resume Log id>, "at": <iso time>}
REBUILD_ASK_MINUTES = 30
FORCE_KEY = "resume.force_ask"  # bot_state: {"job": <page id>, "text": <refused instruction>}
MAX_GAP_BUTTONS = 2
REBUILD_ASK = (
    "What should change? Send your instructions as your next message, for example:\n"
    "- put Terraform first in the skills\n"
    "- drop Oracle\n"
    "- use the word observability in the monitoring bullet\n"
    "Only skills you have (Skills Inventory, Term Map) can be added.\n"
    "Ref {ref}"
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MATRIX_LINE = re.compile(r"^(Strong|Transferable|Gap) \| ")
MAX_WAITING_LIST = 10
REFERENCE_TTL_SECONDS = 600
NO_TARGET = "DRY RUN: no Job Opportunities target in this environment, nothing to show."
MAX_TEXT = 4000  # Telegram allows 4096 characters per message
HIGH_ALERT = "\U0001F525 Apply high: apply today while the posting is fresh."
MAX_HIGH_ALERTS = 5
MAX_KEYWORDS = 6  # missing keywords shown on a /pending card
MAX_REWORD = 4  # cards sent at once after a screening; the rest are in /pending


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (label, callback data)
    document: tuple[str, bytes] | None = None  # (filename, PDF bytes); text is the caption


def _contact_ids(contacts: list[Any]) -> list[str]:
    return [c.page_id for c in contacts if getattr(c, "page_id", None)]


def short_id(page_id: str) -> str:
    """Page ID for callback data: a Notion UUID without hyphens (32 characters)."""
    return page_id.replace("-", "") if UUID_RE.match(page_id) else page_id


REQUEST_RE = re.compile(
    r"^\s*(?:please\s+)?(?:add|include|put|list|mention)\s+(.+?)"
    r"(?:\s+(?:to|in|into|on|under)\s+(?:the\s+|that\s+|my\s+)?"
    r"(?:skills?|resume|cv|table|list|section|it)\b.*)?\s*[.!]?\s*$", re.IGNORECASE)
FILLER = {"that", "the", "this", "a", "an", "my", "also"}


def requested_terms(text: str, known: str | None = None) -> list[str]:
    """Skill names from an instruction like "add that powershell in that resume", spelled as
    in `known` (the job's Gaps) when they match: ["PowerShell"]."""
    match = REQUEST_RE.match(text or "")
    if not match:
        return []
    spelled = {g.strip().casefold(): g.strip() for g in (known or "").split(",") if g.strip()}
    terms = []
    for part in re.split(r",|\band\b|&", match.group(1)):
        words = [w for w in part.split() if w.casefold() not in FILLER]
        term = " ".join(words).strip(" .")
        if term:
            terms.append(spelled.get(term.casefold(), term))
    return terms


def same_page(a: str, b: str) -> bool:
    return short_id(a) == short_id(b)


def match_counts(body: list[str]) -> dict[str, int]:
    """Strong, Transferable and Gap counts from the newest "Screening (" section."""
    start = max((i for i, b in enumerate(body) if b.startswith(SECTION_PREFIX)), default=None)
    counts = {"Strong": 0, "Transferable": 0, "Gap": 0}
    if start is None:
        return counts
    for line in body[start + 1:]:
        match = MATRIX_LINE.match(line)
        if match:
            counts[match.group(1)] += 1
    return counts


ONE_AT_A_TIME = ("Check the resume: tap Approve resume, or Rebuild to change it. Then you "
                 "get the job link to apply, and the next job comes after I applied or Not "
                 "applying.")
FETCH_HINT = ("After your last job: /fetchcontacts finds the contacts and writes the Gmail "
              "drafts, with the resume attached, for every job you applied to today.")
NOT_APPLYING_ASK = "Why are you not applying? (It goes to Notion.)"
# code -> (what you tapped, the Skip reason option)
NOT_APPLYING = {"c": ("the job is closed or expired", "Expired"),
                "f": ("not a fit after all", "Other"),
                "o": ("another reason", "Other")}


class Desk:
    def __init__(
        self,
        s: Settings,
        deps: ScreenDeps,
        state: BotState,
        today: Callable[[], date] = date.today,
        now: Callable[[], datetime] = datetime.now,
        resume: ResumeDeps | None = None,
        contacts: ContactDeps | None = None,
        mail: MailDeps | None = None,
        track: TrackDeps | None = None,
        track_now: Callable[[], datetime] | None = None,
        strategy: StrategyDeps | None = None,
    ):
        self.s = s
        self.deps = deps
        self.resume = resume
        self.contacts = contacts
        self.mail = mail
        self.track = track
        self._track_now = track_now
        self.strategy = strategy
        self.state = state
        # Set by the bot for each update: sends a reply at once (before a long build).
        self.notify: Callable[[Reply], None] | None = None
        self.today = today
        self.now = now
        window = int(s.screening.get("jd_capture_minutes", jd_capture.WINDOW_MINUTES))
        self.captures = Captures(state, window)
        self._reference: tuple[float, Reference] | None = None
        self._ranker: Ranker | None = None
        # Reads one public page (the employer's link behind an Adzuna job).
        self.get_page: Callable[[str], tuple[str, str]] = \
            lambda url: http.get_page(url, s=self.s)

    @property
    def repo(self) -> JobsRepo | None:
        return self.deps.repo

    def reference(self) -> Reference:
        stamp = self.now().timestamp()
        if self._reference is None or stamp - self._reference[0] > REFERENCE_TTL_SECONDS:
            self._reference = (stamp, self.deps.reference())
        return self._reference[1]

    # ------------------------------------------------------------ /pending

    def ranked(self) -> list[JobRow]:
        assert self.repo is not None
        ref, today = self.reference(), self.today()
        rows = pending_rows(self.repo)
        return sorted(rows, key=lambda r: rank_key(r, ref, today), reverse=True)

    def ranker(self) -> Ranker:
        """The free skill matcher (sweep/rank.py), rebuilt when the reference data is."""
        ref = self.reference()
        if self._ranker is None or self._ranker.ref is not ref:
            self._ranker = Ranker(ref)
        return self._ranker

    def keyword_lines(self, body: list[str]) -> list[str]:
        """Skill match, missing keywords and the job's words for skills you have (no AI)."""
        jd, _kind = description(body)
        if not jd:
            return []
        found = self.ranker().keywords(jd)
        lines = []
        if found.share is not None:
            named = len(found.have) + len(found.missing)
            lines.append(f"Skill match: {found.share}% ({len(found.have)} of {named} tools "
                         "it names)")
        if found.missing:
            more = len(found.missing) - MAX_KEYWORDS
            extra = f" and {more} more" if more > 0 else ""
            lines.append(f"Missing keywords: {', '.join(found.missing[:MAX_KEYWORDS])}{extra}")
        if found.reword:
            pairs = "; ".join(f"{theirs} (your {mine})"
                              for theirs, mine in found.reword[:MAX_REWORD])
            lines.append(f"Use their word: {pairs}")
        return lines

    def card(self, row: JobRow, index: int, total: int, buttons: bool = True) -> Reply:
        assert self.repo is not None
        body = self.repo.read_body(row.page_id)
        counts = match_counts(body)
        posted = row.posted_date.isoformat() if row.posted_date else "unknown"
        place = ", ".join(p for p in (row.city, row.country) if p) or "unknown place"
        lines = [
            f"[{index + 1}/{total}] {row.screen_verdict}",
            f"{row.company}, {row.role}",
            f"{place} | {row.board or 'unknown board'} | posted {posted} | "
            f"ghost {row.ghost_risk or 'Unknown'}",
            f"Contract {row.contract_type or 'Unknown'} | Sponsorship "
            f"{row.sponsorship or 'Not mentioned'} | Visa: {', '.join(row.visa_flags) or 'none'}",
            f"Match: {counts['Strong']} strong, {counts['Transferable']} transferable, "
            f"{counts['Gap']} gaps",
            *self.keyword_lines(body),
            f"Gaps: {row.gaps or 'none'}",
        ]
        if row.url:
            lines.append(row.url)
        keys = []
        if buttons:
            pid = short_id(row.page_id)
            keys = [("Approve", f"ap:{pid}"), ("Skip", f"sk:{pid}"), ("Next", f"nx:{index + 1}")]
        return Reply("\n".join(lines), keys)

    def waiting_line(self) -> str | None:
        assert self.repo is not None
        count = len(waiting_for_jd(self.repo))
        if not count:
            return None
        return f"{count} LinkedIn job{'s need' if count > 1 else ' needs'} the JD: /jd"

    def pending(self, index: int = 0, rows: list[JobRow] | None = None) -> list[Reply]:
        """The card at `index`. `rows` reuses a ranking already read (a button tap)."""
        if self.repo is None:
            return [Reply(NO_TARGET)]
        rows = self.ranked() if rows is None else rows
        waiting = self.waiting_line()
        if not rows:
            text = "Nothing to review right now."
            return [Reply("\n".join(t for t in (text, waiting) if t))]
        index = index % len(rows)
        reply = self.card(rows[index], index, len(rows))
        if waiting:
            reply.text += f"\n\n{waiting}"
        return [reply]

    def tap(self, data: str) -> list[Reply]:
        """A button press: ap:<id>, sk:<id> or nx:<index>."""
        if self.repo is None:
            return [Reply(NO_TARGET)]
        action, _, arg = data.partition(":")
        if action == "nx":
            return self.pending(int(arg) if arg.isdigit() else 0)
        if action in ("ra", "rb", "rq", "ia", "na", "nr", "fg", "fc") and arg:
            return self.resume_tap(action, arg)
        if action in ("rc", "lk", "rd") and arg:
            return self.track_tap(action, data)
        if action in ("sa", "sr", "sc") and arg:
            return self.strategy_tap(action, arg)
        if action == "oc" and arg:
            return self.outreach_tap(arg)
        if action == "fx" and arg:  # "Go" under /fetchcontacts
            return self.fetch_contacts_tap(arg)
        if action == "ct" and arg:  # "Find contacts" (older messages; /contacts does the same)
            from jobengine.resume.builder import notion_id

            return self.outreach_or_apply_only(notion_id(arg))
        if action == "dr" and arg:  # "Write Gmail drafts" under the contacts found
            if self.mail is None:
                return [Reply("Gmail drafts are not available in this bot.")]
            from jobengine.resume.builder import notion_id

            job_id = notion_id(arg)
            if self.sending():
                self.say(Reply("Writing, checking and sending the mails..."))
            else:
                self.say(Reply("Writing Gmail drafts (never sent)..."))
            return self.drafts(job_id)
        if action not in ("ap", "sk") or not arg:
            return [Reply("That button is no longer valid. Send /pending.")]
        before = self.ranked()
        position = next((i for i, r in enumerate(before) if same_page(r.page_id, arg)), 0)
        listed = next((r for r in before if same_page(r.page_id, arg)), None)
        rest = [r for r in before if r is not listed]
        if action == "sk" and listed is not None:
            # Still in the review list, so still Screened: skip without reading the page again.
            job = f"{listed.company}, {listed.role}"
            self.repo.update(listed.page_id, {"Status": "Declined"})
            return [Reply(f"Skipped: {job}"), *self.pending(position, rest)]
        values = self.repo.get_values(arg)
        status = (values or {}).get("Status")
        if action == "ap" and self.resume and status in resume_builder.BUILDABLE_STATUSES:
            # A second Approve tap: show the latest preview instead of building again.
            return self.after_build(self.on_job_approved(arg, values or {}), position)
        if not values or status != "Screened":
            return [Reply("Already handled"), *self.pending(position)]
        page_id = next((r.page_id for r in before if same_page(r.page_id, arg)), arg)
        job = f"{values.get('Company')}, {values.get('Role')}"
        if action == "ap":
            self.repo.update(page_id, {"Status": "Approved"})
            self.say(Reply(APPROVED_TEXT.format(job=job)))
            return self.after_build(self.on_job_approved(page_id, values), position, rest)
        self.repo.update(page_id, {"Status": "Declined"})
        return [Reply(f"Skipped: {job}"), *self.pending(position, rest)]

    def after_build(self, replies: list[Reply], position: int = 0,
                    rest: list[JobRow] | None = None) -> list[Reply]:
        """One job at a time: while its resume waits for you (Approve resume or Rebuild) and
        then for I applied or Not applying, the next job is not shown. A build that failed
        leaves nothing to review, so the next job comes at once."""
        if self.resume is None or not any(r.document for r in replies):
            return [*replies, *self.pending(position, rest)]
        return [*replies, Reply(ONE_AT_A_TIME)]

    # ------------------------------------------------------------ resumes (Module 04)

    def say(self, reply: Reply) -> None:
        if self.notify is not None:
            self.notify(reply)

    def on_job_approved(
        self, page_id: str, values: dict[str, Any], *, correction: str | None = None,
        force: bool = False, force_terms: list[str] | None = None,
    ) -> list[Reply]:
        """Build (or show) the resume for an approved job."""
        if self.resume is None:
            return [Reply(NO_RESUME)]
        self.say(Reply(f"Building resume for {values.get('Company')}..."))
        outcome = resume_builder.build_resume(
            self.resume, page_id, correction=correction, force=force, force_terms=force_terms)
        if outcome.status == "correction_refused" and correction:
            # "Add anyway": list it in the skills section; you learn it before the interview.
            self.state.set(FORCE_KEY, {"job": page_id, "text": correction})
            return [Reply(outcome.message,
                          [("Add anyway (I'll learn it)", f"fc:{short_id(page_id)}")])]
        return [self.preview(outcome)]

    def preview(self, outcome: BuildOutcome) -> Reply:
        if not outcome.ok or outcome.pdf is None:
            return Reply(outcome.message)
        name = outcome.filename or "resume.pdf"
        buttons = [("Rebuild", f"rb:{short_id(outcome.job_id)}")]
        if outcome.log_id:
            log = outcome.log_id.replace("-", "")
            buttons = [("Approve resume", f"ra:{log}"), ("Rebuild", f"rq:{log}")]
        plan = outcome.plan
        forced = {f.casefold() for f in (plan.forced_skills if plan else [])}
        gaps = [g for g in (plan.gaps_reported if plan else []) if g.casefold() not in forced]
        for gap in gaps[:MAX_GAP_BUTTONS]:
            data = f"fg:{short_id(outcome.job_id)}:{gap}"
            if len(data.encode()) <= 64:  # Telegram's limit for button data
                buttons.append((f"Add {gap}", data))
        return Reply(outcome.caption or "", buttons, document=(name, outcome.pdf))

    def resume_tap(self, action: str, arg: str) -> list[Reply]:
        if self.resume is None:
            return [Reply(NO_RESUME)]
        if action == "rb":
            self.state.delete(REBUILD_KEY)
            values = (self.repo.get_values(arg) if self.repo else None) or {}
            return self.on_job_approved(arg, values, force=True)
        if action == "fg":  # "Add <gap>" under a preview
            job_id, _, term = arg.partition(":")
            values = (self.repo.get_values(job_id) if self.repo else None) or {}
            return self.on_job_approved(job_id, values, correction=f"add {term} to the skills",
                                        force=True, force_terms=[term])
        if action == "fc":  # "Add anyway" under a refused instruction
            ask = self.state.get(FORCE_KEY) or {}
            if not ask.get("text") or not same_page(str(ask.get("job")), arg):
                return [Reply("That request is no longer open. Send the instruction again.")]
            self.state.delete(FORCE_KEY)
            values = (self.repo.get_values(arg) if self.repo else None) or {}
            terms = requested_terms(ask["text"], values.get("Gaps"))
            return self.on_job_approved(arg, values, correction=ask["text"], force=True,
                                        force_terms=terms)
        if action == "rq":
            # Ask what to change: a reply to this message (it carries the Ref) is a correction.
            job_id = resume_builder.job_for_log(self.resume, arg)
            if not job_id:
                return [Reply("That resume preview is no longer in Resume Log.")]
            # The next plain message (within 30 minutes) is the correction; a reply works too.
            self.state.set(REBUILD_KEY, {"log": arg, "at": self.now().isoformat()})
            return [Reply(REBUILD_ASK.format(ref=resume_builder.short_ref(arg)),
                          [("Rebuild as it is", f"rb:{short_id(job_id)}")])]
        if action == "ia":
            return [Reply(resume_builder.mark_applied(self.resume, arg)), *self.next_job()]
        if action == "na":  # "Not applying": why?
            return [Reply(NOT_APPLYING_ASK, [(label, f"nr:{arg}:{code}")
                                             for code, (label, _) in NOT_APPLYING.items()])]
        if action == "nr":
            job_id, _, code = arg.partition(":")
            return [Reply(self.not_applying(job_id, code)), *self.next_job()]
        result = resume_builder.finalise(self.resume, arg)
        if result.status == "newer":
            latest = [self.preview(result.latest)] if result.latest else []
            return [Reply(result.message), *latest]
        if result.status == "approved" and result.job_id:
            # Nothing runs by itself: the next steps are buttons.
            job = short_id(result.job_id)
            buttons = [("I applied", f"ia:{job}"), ("Not applying", f"na:{job}")]
            text = self.apply_link_text(result.job_id, result.job_url, result.message)
            text += ("\nWhen you are done, tap I applied (or Not applying): then the next job "
                     "comes.")
            if self.contacts is not None and self.mail is not None:
                text += f"\n{FETCH_HINT}"
            return [*self.apply_pack(result.job_id), Reply(text, buttons)]
        return [Reply(result.message)]

    def apply_link_text(self, job_id: str, url: str | None, text: str) -> str:
        """An Adzuna link is swapped for the employer's own page (also on the job's URL, the
        Adzuna link noted on the page). When that fails, a way around Adzuna's region block."""
        from jobengine.apply import link

        if not url or not link.is_adzuna(url) or self.repo is None:
            return text
        found = link.resolve(url, self.get_page)
        values = self.repo.get_values(job_id) or {}
        if found.employer:
            if self.deps.write:
                try:
                    self.repo.update(job_id, {"URL": found.url})
                    self.repo.append_body(job_id, [f"Adzuna link: {url}"])
                except http.HttpError as exc:
                    log.warning("could not save the employer link on %s: %s", job_id, exc)
            return (text.replace(url, found.url) + "\n(The employer's own page. The Adzuna "
                    "link is kept on the Notion page.)")
        return text + "\n" + link.blocked_note(values.get("Company") or "",
                                               values.get("Role") or "",
                                               values.get("Country") or "")

    def next_job(self) -> list[Reply]:
        """After I applied or Not applying: the next job to review, if any."""
        if self.repo is None:
            return []
        return self.pending(0)

    def not_applying(self, job_id: str, code: str) -> str:
        """Not applying after the resume: Status Declined, the reason in Skip reason and a
        note on the page, so the job leaves every list."""
        from jobengine.resume.builder import notion_id

        if self.repo is None:
            return NO_TARGET
        job_id = notion_id(job_id)
        values = self.repo.get_values(job_id)
        if not values:
            return "That job is no longer in Job Opportunities."
        if values.get("Status") not in resume_builder.BUILDABLE_STATUSES:
            return f"Already handled (Status is {values.get('Status') or 'empty'})."
        label, reason = NOT_APPLYING.get(code, NOT_APPLYING["o"])
        self.repo.update(job_id, {"Status": "Declined", "Skip reason": reason,
                                  "Last activity date": self.today()})
        if self.deps.write:
            try:
                self.repo.append_body(job_id, [
                    f"Not applied ({self.today().isoformat()}): {label}"])
            except http.HttpError as exc:
                log.warning("could not note the reason on %s: %s", job_id, exc)
        return f"Marked as not applied: {values.get('Company')}, {values.get('Role')} ({label})."

    # ------------------------------------------------------------ Apply pack (PR 11)

    def apply_pack(self, job_id: str) -> list[Reply]:
        """Ready answers for this job's portal form; also saved on the job's Notion page."""
        from jobengine.apply import pack
        from jobengine.resume.builder import specific_details

        if self.repo is None:
            return []
        values = self.repo.get_values(job_id)
        if not values:
            return []
        text = pack.build(self.s, self.deps.config(), values,
                          specific_details(self.repo.read_body(job_id)), self.today())
        if self.deps.write:
            try:
                self.repo.append_body(job_id, pack.blocks(text))
            except http.HttpError as exc:  # the answers still reach you in Telegram
                log.warning("could not save the apply pack on %s: %s", job_id, exc)
        return [Reply(text[:MAX_TEXT])]

    def applypack_command(self, args: str) -> list[Reply]:
        """/applypack <job>: the Apply pack again."""
        if self.repo is None:
            return [Reply(NO_TARGET)]
        if not args.strip():
            return [Reply("Send /applypack <job URL or page id>.")]
        row = find_row(self.repo, args.strip())
        if row is None:
            return [Reply(f"No Job Opportunities row found for {args.strip()}")]
        return self.apply_pack(row.page_id)

    # ------------------------------------------------------------ outreach budget (Module 10)

    def outreach_or_apply_only(self, job_id: str) -> list[Reply]:
        """After a resume is approved: paid contact lookup and cold mails for the best-ranked
        jobs within this week's credit budget; free contacts only for the rest."""
        if self.contacts is None:
            return []
        from jobengine.outreach import planner

        values = (self.repo.get_values(job_id) if self.repo else None) or {}
        c = self.contacts
        decision = planner.plan(c.state, c.config(), c.reference(), values, job_id, c.today())
        company = values.get("Company") or "this job"
        if decision.outreach:
            line = Reply(f"Outreach for {company}: yes ({decision.reason}; "
                         f"{max(decision.left - 1, 0)} of {decision.allowance} left this week).")
        else:
            line = Reply(f"Apply only for {company}: {decision.reason}. Free contacts (job "
                         "posting, Contacts cache) are still used; no credits are spent.",
                         [("Find contacts anyway", f"oc:{short_id(job_id)}")])
        if self.notify is None:
            return [line, *self.find_contacts(job_id, paid=decision.outreach)]
        self.say(line)
        return self.find_contacts(job_id, paid=decision.outreach)

    def outreach_tap(self, arg: str) -> list[Reply]:
        """oc:<job>: Find contacts anyway (counts against this week's budget)."""
        if self.contacts is None:
            return [Reply("Contact lookup is not available in this bot.")]
        from jobengine.outreach import planner
        from jobengine.resume.builder import notion_id

        job_id = notion_id(arg)
        c = self.contacts
        planner.force(c.state, c.config(), job_id, c.today())
        return self.find_contacts(job_id)

    def outreach_command(self) -> list[Reply]:
        if self.contacts is None:
            return [Reply("Contact lookup is not available in this bot.")]
        from jobengine.outreach import planner

        c = self.contacts
        return [Reply(planner.status_text(c.state, c.config(), c.today()))]

    # ------------------------------------------------------------ contacts (Module 05)

    def find_contacts(self, job_id: str, paid: bool = True) -> list[Reply]:
        """Contact lookup after a resume is approved, and for /contacts. `paid` False: free
        sources only (an apply-only job)."""
        if self.contacts is None:
            return []
        values = (self.repo.get_values(job_id) if self.repo else None) or {}
        what = "Finding contacts" if paid else "Checking free contacts"
        self.say(Reply(f"{what} for {values.get('Company') or 'this job'}..."))
        result = contact_finder.find_contacts(self.contacts, job_id, paid=paid)
        if result.status == "done":
            return self._then_drafts(Reply(result.message), job_id, result.contacts)
        return [Reply(result.message)]

    def _then_drafts(self, first: Reply, job_id: str, contacts: list[Any]) -> list[Reply]:
        """The contacts summary with a "Write Gmail drafts" button: drafts are only written
        when you tap it."""
        contact_finder.on_contacts_ready(job_id, contacts)
        if self.mail is not None and _contact_ids(contacts):
            if self.sending():
                first.buttons = [*first.buttons, ("Check and send mails",
                                                  f"dr:{short_id(job_id)}")]
                first.text += ("\nNext: tap Check and send mails (mail mode send: each mail is "
                               "checked and sent only when every check passes).")
            else:
                first.buttons = [*first.buttons, ("Write Gmail drafts",
                                                  f"dr:{short_id(job_id)}")]
                first.text += "\nNext: tap Write Gmail drafts (they are never sent by themselves)."
        return [first]

    # ------------------------------------------------------------ /fetchcontacts

    def applied_on(self, day: date) -> list[tuple[str, dict[str, Any]]]:
        """Jobs you marked I applied on this day, oldest row first."""
        from jobengine.notion_repo import select_filter

        assert self.repo is not None
        return [(pid, values) for pid, values in
                self.repo.query_rows(select_filter("Status", "Applied"))
                if values.get("Applied date") == day]

    def fetch_contacts_command(self) -> list[Reply]:
        """/fetchcontacts: what a run for today's applied jobs would do, and a Go button.
        Nothing is looked up or written before you tap Go."""
        if self.contacts is None or self.mail is None or self.repo is None:
            return [Reply("Contact lookup and Gmail drafts are not available in this bot.")]
        today = self.today()
        jobs = self.applied_on(today)
        if not jobs:
            return [Reply(f"No job is marked I applied today ({today.isoformat()}). Tap I "
                          "applied under a job first.")]
        saved = [(pid, v) for pid, v in jobs if v.get("Contacts")]
        lines = [f"Applied today ({today.isoformat()}): {len(jobs)} job(s)."]
        for _pid, values in jobs:
            count = len(values.get("Contacts") or [])
            what = f"{count} saved contact(s)" if count else "needs a lookup"
            lines.append(f"- {values.get('Company')}, {values.get('Role')}: {what}")
        need = len(jobs) - len(saved)
        if need:
            lines.append(f"{need} job(s) need a lookup: the Contacts cache and the job posting "
                         "first, then the paid providers (/credits shows what is left).")
            if self.s.app_env != "prod" or self.s.dry_run:
                lines.append("Paid calls are off here (only prod with DRY_RUN=false); lookups "
                             "use test data.")
        if self.sending():
            lines.append("Mail mode is send: each mail is checked and sent (in the "
                         "recipient's morning) only when every check passes.")
        else:
            lines.append("Then Gmail drafts with the resume attached (never sent by "
                         "themselves). Drafts already written are skipped.")
        lines.append("Tap Go to start.")
        return [Reply("\n".join(lines), [("Go", f"fx:{today.isoformat()}")])]

    def fetch_contacts_tap(self, day: str) -> list[Reply]:
        """fx:<date>: contacts, then drafts, for every job applied on that day. One short line
        per job as it goes, then a summary."""
        if self.contacts is None or self.mail is None or self.repo is None:
            return [Reply("Contact lookup and Gmail drafts are not available in this bot.")]
        try:
            when = date.fromisoformat(day)
        except ValueError:
            return [Reply("That button is no longer valid. Send /fetchcontacts.")]
        if when != self.today():
            return [Reply("That list was for another day. Send /fetchcontacts again.")]
        from jobengine.outreach import planner

        jobs = self.applied_on(when)
        out: list[Reply] = []

        def emit(reply: Reply) -> None:
            if self.notify is None:
                out.append(reply)
            else:
                self.notify(reply)

        drafted, dry = 0, False
        none: list[str] = []
        waiting: list[str] = []
        failed: list[str] = []
        c = self.contacts
        for index, (job_id, values) in enumerate(jobs, 1):
            job = f"{values.get('Company')}, {values.get('Role')}"
            emit(Reply(f"{index}/{len(jobs)} {job}..."))
            if not values.get("Contacts"):
                planner.force(c.state, c.config(), job_id, c.today())  # still counted
                found = contact_finder.find_contacts(c, job_id)
                if found.status == "waiting_domain":
                    emit(Reply(found.message))  # reply to it; then tap Write Gmail drafts
                    waiting.append(job)
                    continue
                if found.status != "done" or not _contact_ids(found.contacts):
                    emit(Reply(found.message[:MAX_TEXT]))
                    none.append(job)
                    continue
                contact_finder.on_contacts_ready(job_id, found.contacts)
            result = mail_drafter.create_drafts(self.mail, job_id)
            if self.sending() and result.ok and result.drafted:
                emit(Reply(mail_sender.send_checked(self.mail, self.state, result)
                           .message[:MAX_TEXT]))
            else:
                emit(Reply(result.message[:MAX_TEXT]))
            dry = dry or result.dry_run
            if result.ok:
                drafted += len(result.drafted)
            else:
                failed.append(job)
        mails = "mail(s) checked for sending" if self.sending() else "Gmail draft(s)"
        lines = [f"Done: {len(jobs)} job(s) applied today, {drafted} {mails}."]
        if dry:
            lines[0] += " DRY RUN: nothing was created in Gmail."
        if none:
            lines.append("No contacts found: " + "; ".join(none))
        if waiting:
            lines.append("Waiting for the email domain (reply to the question, then tap Write "
                         "Gmail drafts): " + "; ".join(waiting))
        if failed:
            lines.append("Drafts not written (see the message above): " + "; ".join(failed))
        return [*out, Reply("\n".join(lines))]

    def contacts_command(self, args: str) -> list[Reply]:
        if self.contacts is None or self.repo is None:
            return [Reply("Contact lookup is not available in this bot.")]
        if not args.strip():
            return [Reply("Send /contacts <job URL or page id>.")]
        row = find_row(self.repo, args.strip())
        if row is None:
            return [Reply(f"No Job Opportunities row found for {args.strip()}")]
        return self.outreach_tap(row.page_id)  # your explicit ask: paid lookup, counted

    def credits(self) -> list[Reply]:
        """/credits: each provider's counter, when it was updated, and whether its key is set."""
        from jobengine.contacts.credits import PROVIDERS, monthly_reset, parse_counter

        config = (self.contacts.config if self.contacts else self.deps.config)()
        keys = {"apollo": self.s.apollo_api_key, "hunter": self.s.hunter_api_key,
                "snov": self.s.snov_client_id and self.s.snov_client_secret,
                "prospeo": self.s.prospeo_api_key,
                "tomba": self.s.tomba_api_key and self.s.tomba_api_secret}
        lines = ["Provider credits (reset on the 1st):"]
        for provider in PROVIDERS:
            key = f"credits.{provider}"
            counter = parse_counter(config.get(key), config.updated(key))
            updated = config.updated(key)
            if counter:
                reset = monthly_reset(counter, self.today())
                value = reset.text() + (" (reset for this month)" if reset.used != counter.used
                                        else "")
            else:
                value = config.get(key) or "not set"
            when = f", updated {updated.isoformat()}" if updated else ""
            key_state = "key set" if keys[provider] else "no key"
            lines.append(f"{provider.capitalize()}: {value}{when}, {key_state}")
        if self.s.app_env != "prod" or self.s.dry_run:
            lines.append("Paid calls are off here (only prod with DRY_RUN=false); lookups use "
                         "test data outside prod.")
        return [Reply("\n".join(lines))]

    def domain_answer(self, replied_to: str, text: str) -> list[Reply] | None:
        """A reply to "What is the email domain for ...? Ref JOB-xxxxxxxx"."""
        if self.contacts is None or "Ref JOB-" not in replied_to:
            return None
        answer = contact_finder.answer_domain(self.contacts, replied_to, text)
        if answer is None:
            return None
        if isinstance(answer, str):
            return [Reply(answer)]
        if answer.status == "done":
            return self._then_drafts(Reply(answer.message), answer.job_id, answer.contacts)
        return [Reply(answer.message)]

    # ------------------------------------------------------------ drafts (Module 06)

    def sending(self) -> bool:
        """Mail mode "send" (/mailmode): drafts that pass every check are sent."""
        return mail_sender.mode(self.state) == mail_sender.SEND

    def drafts(self, job_id: str, ids: list[str] | None = None) -> list[Reply]:
        assert self.mail is not None
        result = mail_drafter.create_drafts(self.mail, job_id, ids)
        if self.sending() and result.ok and result.drafted:
            sent = mail_sender.send_checked(self.mail, self.state, result)
            return [Reply(sent.message[:MAX_TEXT])]
        return [Reply(result.message[:MAX_TEXT])]

    def mail_queue_tick(self) -> list[Reply]:
        """Every few minutes: send one queued mail whose recipient's morning has come."""
        if self.mail is None or not self.sending() and not mail_timing.load(self.state):
            return []
        text = mail_sender.send_due(self.mail, self.state)
        return [Reply(text[:MAX_TEXT])] if text else []

    def mailqueue_command(self) -> list[Reply]:
        if self.mail is None:
            return [Reply("Gmail drafts are not available in this bot.")]
        return [Reply(mail_sender.queue_text(self.mail, self.state)[:MAX_TEXT])]

    def mailmode_command(self, args: str) -> list[Reply]:
        return [Reply(mail_sender.mode_command(self.state, args, self.mail))]

    def drafts_command(self, args: str) -> list[Reply]:
        """/drafts <job>: retry drafting for a job (drafts already made are skipped)."""
        if self.mail is None or self.repo is None:
            return [Reply("Gmail drafts are not available in this bot.")]
        if not args.strip():
            return [Reply("Send /drafts <job URL or page id>.")]
        row = find_row(self.repo, args.strip())
        if row is None:
            return [Reply(f"No Job Opportunities row found for {args.strip()}")]
        return self.drafts(row.page_id)

    def alertcheck_command(self) -> list[Reply]:
        """/alertcheck: the alert emails the next /fetch reads and the jobs in each."""
        from jobengine.sweep import fakes
        from jobengine.sweep.sources import gmail_alerts

        fake = isinstance(self.repo, FakeJobsRepo)
        text = gmail_alerts.check(self.s, fakes.gmail_messages if fake else None)
        return [Reply(text[:MAX_TEXT])]

    def gaps_command(self, limit: int = 15) -> list[Reply]:
        """Skills jobs asked for that you do not have yet, most common first (from the
        Gaps column that screening and resume building fill)."""
        if self.repo is None:
            return [Reply(NO_TARGET)]
        counts: dict[str, list[str]] = {}
        names: dict[str, str] = {}
        rows = 0
        for _pid, values in self.repo.query_rows():
            gaps = [g.strip() for g in str(values.get("Gaps") or "").split(",") if g.strip()]
            if not gaps:
                continue
            rows += 1
            company = values.get("Company") or "?"
            for gap in dict.fromkeys(g.casefold() for g in gaps):
                names.setdefault(gap, next(g for g in gaps if g.casefold() == gap))
                counts.setdefault(gap, []).append(company)
        if not counts:
            return [Reply("No gaps recorded yet. Screening and resume building add them.")]
        ranked = sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]
        lines = [f"Skill gaps across {rows} jobs (most common first):"]
        for i, (key, companies) in enumerate(ranked, 1):
            shown = ", ".join(dict.fromkeys(companies[:3]))
            more = f" +{len(companies) - 3}" if len(companies) > 3 else ""
            jobs = f"{len(companies)} job{'s' if len(companies) != 1 else ''}"
            lines.append(f"{i}. {names[key]}: {jobs} ({shown}{more})")
        lines.append("")
        lines.append("Learned one? Add it to Skills Inventory as Hands-on or Production: "
                     "screening and resumes then count it as yours.")
        return [Reply("\n".join(lines)[:MAX_TEXT])]

    def correction(self, replied_to: str, text: str) -> list[Reply] | None:
        """A reply to a preview ("Ref RL-xxxxxxxx" in it) builds the next revision. None when
        the replied-to message is not a preview."""
        if self.resume is None or "Ref RL-" not in replied_to:
            return None
        self.state.delete(REBUILD_KEY)
        log_id = resume_builder.find_log_by_ref(self.resume, replied_to)
        job_id = resume_builder.job_for_log(self.resume, log_id) if log_id else None
        if not job_id:
            return [Reply("That resume preview is no longer in Resume Log.")]
        values = (self.repo.get_values(job_id) if self.repo else None) or {}
        return self.on_job_approved(job_id, values, correction=text.strip(), force=True)

    # ------------------------------------------------------------ tracking (Module 07)

    def tracking_now(self) -> datetime:
        """Now, with the configured UTC offset (default +05:30) when the clock is naive."""
        now = (self._track_now or self.now)()
        if now.tzinfo is not None:
            return now
        sign, _, hm = str(self.s.tracking.get("utc_offset", "+05:30")).partition("+")
        hours, _, minutes = (hm or sign.lstrip("-")).partition(":")
        delta = timedelta(hours=int(hours or 0), minutes=int(minutes or 0))
        return now.replace(tzinfo=timezone(-delta if sign.startswith("-") else delta))

    def _no_track(self) -> list[Reply] | None:
        return [Reply("Tracking is not available in this bot.")] if self.track is None else None

    def report_replies(self, report: DailyReport) -> list[Reply]:
        replies = [Reply(report.text()[:MAX_TEXT])]
        replies += [Reply(card.text[:MAX_TEXT], card.buttons) for card in report.cards]
        if report.retention is not None:
            replies.append(Reply(report.retention.text, report.retention.buttons))
        return replies

    def today_command(self) -> list[Reply]:
        """/today: the daily check now."""
        if self.track is None:
            return self._no_track() or []
        self.say(Reply("Running the daily check..."))
        return self.report_replies(track_runner.run_daily(self.track, self.tracking_now()))

    def reply_ping_tick(self) -> list[Reply]:
        """Every few minutes: ping new replies from contacts and employers (track/ping.py).
        A Gmail or Notion failure is only logged: the daily check reports token problems."""
        from jobengine.track import ping

        if self.track is None:
            return []
        now = self.tracking_now()
        if not ping.due(self.track, now):
            return []
        try:
            texts = ping.check(self.track, now)
        except Exception as exc:
            log.warning("reply check failed: %s", exc)
            return []
        return [Reply(text[:MAX_TEXT]) for text in texts]

    def track_tap(self, action: str, data: str) -> list[Reply]:
        if self.track is None:
            return self._no_track() or []
        now = self.tracking_now()
        if action == "rc":
            return [Reply(track_runner.apply_choice(self.track, now, data))]
        if action == "lk":
            return [Reply(track_runner.link_choice(self.track, now, data))]
        return [Reply(track_runner.retention_choice(self.track, now, data))]

    def followups_command(self) -> list[Reply]:
        if self.track is None:
            return self._no_track() or []
        return [Reply(track_commands.followups_text(self.track, self.tracking_now().date()))]

    def stats_command(self) -> list[Reply]:
        from jobengine.track.stats import stats_text

        if self.track is None:
            return self._no_track() or []
        jobs = [v for _, v in self.track.jobs.query_rows()] if self.track.jobs else []
        contacts = [v for _, v in self.track.contacts.all_rows()] if self.track.contacts else []
        return [Reply(stats_text(jobs, contacts, self.tracking_now().date())[:MAX_TEXT])]

    def sources_command(self) -> list[Reply]:
        if self.track is None:
            return self._no_track() or []
        return [Reply(track_commands.sources_text(self.track))]

    def health_command(self) -> list[Reply]:
        if self.track is None:
            return self._no_track() or []
        return [Reply(track_commands.health_text(self.track))]

    def digest_command(self) -> list[Reply]:
        if self.track is None:
            return self._no_track() or []
        return [Reply(run_weekly_digest(self.track, self.tracking_now()))]

    def status_lines(self) -> str | None:
        return track_commands.status_lines(self.track) if self.track is not None else None

    # ------------------------------------------------------------ strategy (Module 08)

    def update_command(self, args: str) -> list[Reply]:
        """/update: research for your review; /update new forces a new run."""
        if self.strategy is None:
            return [Reply("/update is not available in this bot.")]
        force = args.strip().lower() == "new"
        if force or not strategy_runner.open_review(self.strategy):
            self.say(Reply(strategy_runner.STARTED))
        result = strategy_runner.run_update(self.strategy, force=force)
        return [*(Reply(m[:MAX_TEXT]) for m in result.messages),
                *(Reply(c.text, c.buttons) for c in result.cards)]

    def strategy_tap(self, action: str, arg: str) -> list[Reply]:
        if self.strategy is None:
            return [Reply("/update is not available in this bot.")]
        if action == "sc":
            messages = strategy_runner.confirm_review(self.strategy, arg)
        else:
            messages = strategy_runner.decide(self.strategy, arg, adopt=action == "sa")
        return [Reply(m[:MAX_TEXT]) for m in messages]

    def strategy_note(self, replied_to: str, text: str) -> list[Reply] | None:
        """A reply to a tip card (Ref ST-...) is stored in that tip's Notes."""
        if self.strategy is None or "Ref ST-" not in replied_to:
            return None
        saved = strategy_runner.add_note(self.strategy, replied_to, text)
        return [Reply(saved)] if saved else None

    def rules_command(self) -> list[Reply]:
        from jobengine.strategy.rules_view import rules_text

        if self.strategy is None:
            return [Reply("/rules is not available in this bot.")]
        d = self.strategy
        text = rules_text(d.config(), d.v16_blocks(), d.existing(), d.today(), d.state, self.s,
                          self.s.resume)
        return [Reply(text[:MAX_TEXT])]

    # ------------------------------------------------------------ /screen and /fetch

    def _summary_text(self, summary: ScreenSummary) -> str:
        assert self.repo is not None
        ready = len(pending_rows(self.repo))
        return f"{summary.text()}\n{ready} ready to review: /pending"

    def screen(self, args: str = "", progress: Callable[[str], None] | None = None) -> str:
        return self._screen(args, progress)[0]

    def screen_replies(self, args: str = "",
                       progress: Callable[[str], None] | None = None) -> list[Reply]:
        """The screening summary, then an alert card for each new Apply high job."""
        text, summary = self._screen(args, progress)
        return [Reply(text), *self.high_alerts(summary)]

    def high_alerts(self, summary: ScreenSummary | None) -> list[Reply]:
        """Apply high jobs this screening found, each with Approve and Skip, so you can
        apply the same day. Jobs already handled are left out."""
        if summary is None or self.repo is None:
            return []
        ids = list(dict.fromkeys(r.page_id for r in summary.results
                                 if r.verdict == "Apply high"))
        replies: list[Reply] = []
        for page_id in ids[:MAX_HIGH_ALERTS]:
            values = self.repo.get_values(page_id)
            if not values or values.get("Status") != "Screened":
                continue
            card = self.card(job_row(page_id, values), len(replies), min(len(ids),
                                                                           MAX_HIGH_ALERTS))
            replies.append(Reply(f"{HIGH_ALERT}\n{card.text}", card.buttons[:2]))
        if len(ids) > MAX_HIGH_ALERTS:
            replies.append(Reply(f"{len(ids) - MAX_HIGH_ALERTS} more Apply high jobs are "
                                 "waiting: /pending"))
        return replies

    def _screen(self, args: str, progress: Callable[[str], None] | None
                ) -> tuple[str, ScreenSummary | None]:
        if self.repo is None:
            return NO_TARGET, None
        word = args.strip().lower()
        try:
            if word == "batch":  # half price, answers later (screen/batch.py)
                summary = batch.submit(self.s, self.deps, self.today())
            elif word == "collect":
                summary = batch.collect(self.s, self.deps, self.today())
            elif args.strip():
                summary = screen_one(self.s, self.deps, self.today(), args.strip())
            else:
                summary = screen_pending(self.s, self.deps, self.today(), progress)
        except (ScreenError, LLMError, http.HttpError) as exc:
            return f"Screening could not run: {exc}", None
        return self._summary_text(summary), summary

    # ------------------------------------------------------------ /autopilot

    def autopilot(self, fetch: autopilot.Fetch | None,
                  progress: Callable[[str], None] | None = None) -> list[Reply]:
        return autopilot.run(self, fetch, progress or (lambda text: None))

    def autopilot_tick(self) -> list[Reply] | None:
        """Called every few minutes by long polling: finishes a waiting /autopilot."""
        return autopilot.tick(self)

    def autopilot_scheduled(self, fetch: autopilot.Fetch | None,
                            progress: Callable[[str], None] | None = None
                            ) -> list[Reply] | None:
        """The morning schedule: carries on with a waiting run, else starts today's run when
        Config schedule.autopilot is true and it is due (screen/schedule.py)."""
        return autopilot.scheduled(self, fetch, progress or (lambda text: None))

    def autopilot_when(self) -> list[Reply]:
        """/autopilot when: the schedule and the next run."""
        from jobengine.screen import schedule

        return [Reply(schedule.status_text(self))]

    # ------------------------------------------------------------ /jd and /done

    def _discarded(self, capture: Capture) -> Reply:
        self.captures.clear()
        minutes = int(self.captures.window.total_seconds() // 60)
        return Reply(
            f"Your /jd paste from {capture.started().strftime('%H:%M')} was older than "
            f"{minutes} minutes and was discarded. Start again with /jd <url>."
        )

    def _create_row(self, capture: Capture) -> str | None:
        assert self.repo is not None
        company = capture.fields.get("company")
        role = capture.fields.get("role")
        if not company or not role:
            return None
        today = self.today()
        country = capture.fields.get("country")
        city = capture.fields.get("city")
        plan: dict[str, Any] = {
            "Company": company,
            "Role": role,
            "URL": capture.url,
            "Board": jd_capture.board_for_url(
                capture.url, (self.s.sweep.get("gmail") or {}).get("sender_boards") or {}
            ),
            "Status": "New",
            "Screen verdict": "Unscreened",
            "First seen": today,
            "Swept date": today,
            "Times seen": 1,
            "Dedupe key": dedupe_key(company, role, city, country or ""),
        }
        posting = jd_capture.linkedin_posting_id(capture.url)
        if posting:
            plan["Posting IDs"] = posting
        if country:
            plan["Country"] = country
        if city:
            plan["City"] = city
        page_id = self.repo.create(plan, [])
        capture.page_id = page_id
        self.captures.save(capture)
        return page_id

    def _find(self, url: str) -> JobRow | None:
        assert self.repo is not None
        posting = jd_capture.linkedin_posting_id(url)
        return find_row(self.repo, posting or url)

    def jd(self, args: str) -> list[Reply]:
        if self.repo is None:
            return [Reply(NO_TARGET)]
        url, rest = jd_capture.parse_jd_command(args)
        if url is None:
            if rest:
                return [Reply("Send /jd <job URL>, then paste the description.")]
            return [self.waiting_list()]
        row = self._find(url)
        capture = self.captures.start(url, self.now(), row.page_id if row else None)
        if rest:
            capture = self.captures.add(capture, rest)
        if row is None:
            self._create_row(capture)
        if capture.page_id is None:
            return [Reply(
                "This job is not in Notion yet. Send the company and role first, as two "
                "lines:\nCompany: <name>\nRole: <title>\nThen paste the description and "
                "send /done."
            )]
        name = f"{row.company}, {row.role}" if row else (
            f"{capture.fields.get('company')}, {capture.fields.get('role')}"
        )
        got = f" Got {capture.chars} characters so far." if capture.chars else ""
        return [Reply(f"Collecting the job description for {name}.{got} Paste the text "
                      "(several messages are fine), then send /done.")]

    def waiting_list(self) -> Reply:
        assert self.repo is not None
        rows = waiting_for_jd(self.repo)
        if not rows:
            return Reply("No jobs are waiting for a description. Use /jd <url> to paste one.")
        rows.sort(key=lambda r: r.swept_date or date.min, reverse=True)
        jobs = f"{len(rows)} jobs wait" if len(rows) > 1 else "1 job waits"
        lines = [f"{jobs} for a description. Send /jd <url> and paste it:"]
        for row in rows[:MAX_WAITING_LIST]:
            lines.append(f"- {row.company}, {row.role}\n  {row.url or '(no URL)'}")
        return Reply("\n".join(lines))

    def text(self, message: str) -> list[Reply] | None:
        """A plain text message. None when no capture is active (the bot answers as usual)."""
        capture = self.captures.get()
        if capture is None:
            return self._rebuild_answer(message)
        if self.captures.expired(capture, self.now()):
            return [self._discarded(capture)]
        capture = self.captures.add(capture, message)
        if capture.page_id is None and self._create_row(capture) is None:
            return [Reply("Still need the company and role as two lines:\n"
                          "Company: <name>\nRole: <title>")]
        return [Reply(f"Got it ({capture.chars} characters so far). Send /done when finished.")]

    def _rebuild_answer(self, message: str) -> list[Reply] | None:
        """The message right after a Rebuild tap is the correction for that resume."""
        ask = self.state.get(REBUILD_KEY)
        if not ask or not ask.get("log"):
            return None
        try:
            asked = datetime.fromisoformat(str(ask.get("at")))
        except ValueError:
            asked = None
        if asked is None or self.now() - asked > timedelta(minutes=REBUILD_ASK_MINUTES):
            self.state.delete(REBUILD_KEY)
            return None
        return self.correction(f"Ref {resume_builder.short_ref(ask['log'])}", message)

    def done(self) -> list[Reply]:
        if self.repo is None:
            return [Reply(NO_TARGET)]
        capture = self.captures.get()
        if capture is None:
            return [Reply("Nothing to finish. Start with /jd <url>.")]
        if self.captures.expired(capture, self.now()):
            return [self._discarded(capture)]
        if capture.page_id is None and self._create_row(capture) is None:
            return [Reply("Send the company and role first, as two lines:\n"
                          "Company: <name>\nRole: <title>")]
        text = capture.text()
        if not text:
            return [Reply("No description collected yet. Paste it, then send /done.")]
        size = int(self.s.screening.get("block_chars", 2000))
        limit = int(self.s.screening.get("max_blocks", 90))
        chunks = [text[i:i + size] for i in range(0, len(text), size)][:limit]
        assert capture.page_id is not None
        self.repo.append_body(capture.page_id, ["Description source: pasted (full)", *chunks])
        self.captures.clear()
        replies = [Reply(f"Saved the description ({len(text)} characters). Screening it now...")]
        replies.append(Reply(self.screen(capture.page_id)))
        replies.extend(self._card_for(capture.page_id))
        return replies

    def _card_for(self, page_id: str) -> list[Reply]:
        assert self.repo is not None
        rows = self.ranked()
        for i, row in enumerate(rows):
            if same_page(row.page_id, page_id):
                return [self.card(row, i, len(rows))]
        values = self.repo.get_values(page_id)
        if not values:
            return []
        row = job_row(page_id, values)
        reason = f" ({values.get('Skip reason')})" if values.get("Skip reason") else ""
        return [Reply(f"{row.company}, {row.role}: {row.screen_verdict}{reason}")]


def fake_desk(s: Settings, today: date, now: Callable[[], datetime] | None = None) -> Desk:
    """In-memory desk for --fake: the sweep and screening fixtures in one Job Opportunities,
    a FakeLLM that says "nothing stated" for rows without a fixture, and in-memory bot_state."""
    from jobengine.llm import FakeLLM
    from jobengine.screen.runner import fake_deps
    from jobengine.sweep import fakes

    deps = fake_deps(s)
    if deps.repo is not None:
        merged = fakes.jobs_repo()
        assert isinstance(deps.repo, FakeJobsRepo)
        merged.rows.update(deps.repo.rows)
        deps.repo = merged
    llm = FakeLLM(default={})  # one instance: a half-price batch is kept until collected
    deps.llm = lambda config: llm
    clock = now or (lambda: datetime.combine(today, datetime.min.time()).replace(hour=9))
    resume = resume_builder.fake_deps(s, jobs=deps.repo, out_dir=ROOT_DIR / "out" / "fake")
    resume.today = lambda: today
    state = FakeBotState()
    contacts = contact_finder.fake_deps(s, jobs=deps.repo, state=state)
    contacts.today = lambda: today
    mail = mail_drafter.fake_deps(s, jobs=deps.repo, contacts=contacts.contacts,
                                  resume_log=resume.resume_log, drive=resume.drive())
    mail.today = lambda: today
    # Tracking works on its own fixture world (fixtures/track), written for FAKE_NOW.
    from jobengine.track.fakes import FAKE_NOW

    track = track_runner.fake_deps(s, state=state)
    strategy = strategy_runner.fake_deps(s, state=state)
    strategy.today = lambda: today
    strategy.ind_deps.today = lambda: today  # type: ignore[attr-defined]
    desk = Desk(s, deps, state, today=lambda: today, now=clock, resume=resume,
                contacts=contacts, mail=mail, track=track, track_now=lambda: FAKE_NOW,
                strategy=strategy)

    def no_network(url: str) -> tuple[str, str]:
        raise http.HttpError(f"GET {url} failed: no network in --fake")

    desk.get_page = no_network
    return desk


def real_desk(s: Settings) -> Desk | None:
    """Notion-backed desk, or None when NOTION_TOKEN is missing."""
    from jobengine.bot_state import bot_state_for
    from jobengine.notion_repo import NotionClient
    from jobengine.screen.runner import real_deps

    if not s.notion_token:
        log.warning("NOTION_TOKEN missing: /pending, /jd and /screen are not available")
        return None
    client = NotionClient(s.notion_token, s)
    state: BotState | None = bot_state_for(s, client)
    if state is None:
        log.warning("bot_state not writable here: /jd pastes are kept in memory only")
        state = FakeBotState()
    try:
        resume = resume_builder.real_deps(s)
    except Exception as exc:  # the bot still screens without the resume builder
        log.warning("resume builder not available: %s", exc)
        resume = None
    try:
        contacts = contact_finder.real_deps(s, state)
    except Exception as exc:  # the bot still works without the contact finder
        log.warning("contact finder not available: %s", exc)
        contacts = None
    try:
        mail = mail_drafter.real_deps(s)
    except Exception as exc:  # the bot still works without Gmail drafts
        log.warning("gmail drafts not available: %s", exc)
        mail = None
    try:
        track = track_runner.real_deps(s, state)
    except Exception as exc:  # the bot still works without tracking
        log.warning("tracking not available: %s", exc)
        track = None
    try:
        strategy = strategy_runner.real_deps(s, state)
    except Exception as exc:  # the bot still works without /update
        log.warning("strategy update not available: %s", exc)
        strategy = None
    from jobengine.llm import set_cost_sink
    from jobengine.track import costs

    set_cost_sink(costs.sink(state, date.today))  # the month's Claude cost (digest)
    return Desk(s, real_deps(s), state, resume=resume, contacts=contacts, mail=mail,
                track=track, strategy=strategy)
