"""/autopilot: from new postings to Gmail drafts in one command.

1. Fetch new jobs (the /fetch sweep).
2. Screen them in a half-price batch (/screen batch). While Anthropic works on it, long
   polling checks it every `screening.autopilot_check_minutes` and carries on by itself; with
   the webhook, /autopilot again carries on.
3. Save the answers (/screen collect), then approve the best Apply high and Apply normal jobs,
   at most `screening.autopilot_approvals` a day. Apply low and Needs review stay in /pending.
4. For each approved job: build the resume and save it (as "Approve resume" does), find
   contacts within this week's outreach budget (paid lookups only in prod with DRY_RUN false),
   and write Gmail drafts. Drafts are never sent.
5. One summary message, then each resume PDF with an "I applied" button.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jobengine import http
from jobengine.contacts import finder as contact_finder
from jobengine.llm import LLMError
from jobengine.mail import drafter as mail_drafter
from jobengine.mail import sender as mail_sender
from jobengine.resume import builder as resume_builder
from jobengine.screen import batch
from jobengine.screen import schedule as autopilot_schedule
from jobengine.screen.models import JobRow
from jobengine.screen.runner import ScreenError

if TYPE_CHECKING:
    from jobengine.screen.desk import Desk, Reply

log = logging.getLogger("jobengine.screen.autopilot")

AUTO_VERDICTS = ("Apply high", "Apply normal")
RUN_KEY = "autopilot.run"  # bot_state: {"since": <iso time>} while its batch works
DAY_KEY = "autopilot.day"  # bot_state: {"date": <iso date>, "count": <jobs approved>}
DEFAULT_APPROVALS = 10
DEFAULT_CHECK_MINUTES = 5

Progress = Callable[[str], None]
Fetch = Callable[[Progress], str]


@dataclass
class JobReport:
    job_id: str
    company: str
    role: str
    verdict: str
    url: str | None
    resume: str = ""
    contacts: str = ""
    drafts: str = ""
    pdf: tuple[str, bytes] | None = None
    approved: bool = False
    pack: list[str] = field(default_factory=list)  # the Apply pack (PR 11)
    mail: str | None = None  # mail mode send: what was sent and what was kept, and why
    ask: str | None = None  # a question to answer by reply (the company's email domain)

    def line(self, number: int) -> str:
        steps = ", ".join(p for p in (self.resume, self.contacts, self.drafts) if p)
        text = f"{number}. {self.company}, {self.role} ({self.verdict}): {steps}"
        return f"{text}\n   {self.url}" if self.url else text


def approvals_limit(desk: Desk) -> int:
    return max(0, int(desk.s.screening.get("autopilot_approvals", DEFAULT_APPROVALS)))


def check_seconds(desk: Desk) -> float:
    minutes = float(desk.s.screening.get("autopilot_check_minutes", DEFAULT_CHECK_MINUTES))
    return max(1.0, minutes) * 60


def approved_today(desk: Desk) -> int:
    day = desk.state.get(DAY_KEY) or {}
    return int(day.get("count") or 0) if day.get("date") == desk.today().isoformat() else 0


def run(desk: Desk, fetch: Fetch | None, progress: Progress) -> list[Reply]:
    """/autopilot. A batch that still waits is collected first, without a new fetch."""
    from jobengine.screen.desk import NO_TARGET, Reply

    if desk.repo is None:
        return [Reply(NO_TARGET)]
    lines: list[str] = []
    try:
        if batch.waiting_rows(desk.deps) or desk.state.get(RUN_KEY):
            lines.append("Autopilot: carrying on with the batch that was sent before "
                         "(no new fetch).")
        else:
            if fetch is not None:
                progress("Fetching new jobs")
                lines.append(fetch(progress))
            progress("Sending the new jobs to the half-price batch")
            summary = batch.submit(desk.s, desk.deps, desk.today())
            lines.append(summary.text().replace(batch.COLLECT_HINT, ""))
        return _collect_then_finish(desk, lines, progress)
    except (ScreenError, LLMError, http.HttpError) as exc:
        lines.append(f"Autopilot stopped: {exc}")
        return [Reply("\n\n".join(lines))]


def tick(desk: Desk) -> list[Reply] | None:
    """Long polling calls this every few minutes: carries on once the batch has ended."""
    if desk.repo is None or not desk.state.get(RUN_KEY):
        return None
    try:
        if batch.ended(desk.deps) is False:
            return None
        return _collect_then_finish(desk, ["Autopilot: the half-price batch has answered."],
                                    lambda text: None)
    except (ScreenError, LLMError, http.HttpError) as exc:
        from jobengine.screen.desk import Reply

        log.warning("autopilot check failed: %s", exc)
        desk.state.delete(RUN_KEY)
        return [Reply(f"Autopilot stopped: {exc}. Send /autopilot to try again.")]


def scheduled(desk: Desk, fetch: Fetch | None, progress: Progress) -> list[Reply] | None:
    """The morning schedule (screen/schedule.py): carry on with a waiting run, else start
    today's run when it is due. None when there is nothing to do or to say."""
    if desk.repo is None:
        return None
    if desk.state.get(RUN_KEY):
        return tick(desk)
    if not autopilot_schedule.due(desk):
        return None
    autopilot_schedule.mark_started(desk)
    replies = run(desk, fetch, progress)
    if replies:
        p = autopilot_schedule.plan(desk)
        replies[0].text = (f"Scheduled autopilot ({p.start:%H:%M} {p.zone.key}):\n\n"
                           + replies[0].text)
    return replies


def _collect_then_finish(desk: Desk, lines: list[str], progress: Progress) -> list[Reply]:
    from jobengine.screen.desk import Reply

    if batch.waiting_rows(desk.deps):
        progress("Checking the half-price batch")
        summary = batch.collect(desk.s, desk.deps, desk.today())
        lines.append(summary.text())
        if batch.waiting_rows(desk.deps):  # still working
            desk.state.set(RUN_KEY, {"since": desk.now().isoformat()})
            lines.append(_wait_text(desk))
            return [Reply("\n\n".join(lines))]
    desk.state.delete(RUN_KEY)
    return _finish(desk, lines, progress)


def _wait_text(desk: Desk) -> str:
    if desk.s.bot_mode == "webhook" and autopilot_schedule.plan(desk).on:
        return ("The scheduled check (every 30 minutes in the morning) carries on by itself "
                "when the answers are in: it approves the best jobs, builds resumes, finds "
                "contacts and writes Gmail drafts. You can also send /autopilot again later.")
    if desk.s.bot_mode == "webhook":
        return ("Send /autopilot again in about an hour: it saves the answers, then approves "
                "the best jobs, builds resumes, finds contacts and writes Gmail drafts.")
    minutes = round(check_seconds(desk) / 60)
    return (f"I check the batch every {minutes} minutes and carry on by myself: save the "
            "answers, approve the best jobs, build resumes, find contacts and write Gmail "
            "drafts. You can also send /autopilot again later.")


def _candidates(desk: Desk) -> tuple[list[JobRow], int]:
    """Apply high and Apply normal jobs, best first, and today's room for approvals."""
    room = max(0, approvals_limit(desk) - approved_today(desk))
    return [r for r in desk.ranked() if r.screen_verdict in AUTO_VERDICTS], room


def _count_approval(desk: Desk) -> None:
    desk.state.set(DAY_KEY, {"date": desk.today().isoformat(),
                             "count": approved_today(desk) + 1})


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else "no details"


def _prepare(desk: Desk, row: JobRow) -> JobReport:
    """Resume, contacts and drafts for one approved job. A failed step stops only this job."""
    assert desk.repo is not None
    report = JobReport(row.page_id, row.company, row.role, row.screen_verdict or "", row.url)
    if desk.resume is None:
        report.resume = "resume building is not available here, left in /pending"
        return report
    desk.repo.update(row.page_id, {"Status": "Approved"})
    built = resume_builder.build_resume(desk.resume, row.page_id)
    if not built.ok or not built.log_id:
        # Back to the review list: it does not use one of today's approvals.
        desk.repo.update(row.page_id, {"Status": "Screened"})
        report.resume = f"resume not built ({_first_line(built.message)}), left in /pending"
        return report
    report.approved = True
    if built.pdf is not None:
        report.pdf = (built.filename or "resume.pdf", built.pdf)
    saved = resume_builder.finalise(desk.resume, built.log_id)
    if saved.status != "approved":
        report.resume = f"resume built, not saved ({_first_line(saved.message)})"
        return report
    report.resume = "resume saved"
    report.pack = [r.text for r in desk.apply_pack(row.page_id)]
    if desk.contacts is None:
        return report
    from jobengine.outreach import planner

    c = desk.contacts
    values = (desk.repo.get_values(row.page_id) if desk.repo else None) or {}
    decision = planner.plan(c.state, c.config(), c.reference(), values, row.page_id, c.today())
    found = contact_finder.find_contacts(c, row.page_id, paid=decision.outreach)
    if found.status == "waiting_domain":
        report.contacts = "contacts wait for the company's email domain (answer below)"
        report.ask = found.message
        return report
    if found.status != "done":
        report.contacts = f"no contacts ({_first_line(found.message)})"
        return report
    contact_finder.on_contacts_ready(row.page_id, found.contacts)
    ids = [x.page_id for x in found.contacts if getattr(x, "page_id", None)]
    report.contacts = f"{len(ids)} contact{'' if len(ids) == 1 else 's'}" + (
        "" if decision.outreach else " (apply only: free sources)")
    if not ids or desk.mail is None:
        return report
    drafts = mail_drafter.create_drafts(desk.mail, row.page_id)
    if not drafts.ok:
        report.drafts = f"no drafts ({_first_line(drafts.message)})"
    elif desk.sending() and drafts.drafted:
        sent = mail_sender.send_checked(desk.mail, desk.state, drafts)
        verb = "would be sent (DRY RUN)" if sent.dry_run else "sent"
        report.drafts = f"{len(sent.sent)} of {len(drafts.drafted)} mails {verb}" + (
            f", {len(sent.kept)} kept as drafts" if sent.kept else "")
        report.mail = sent.message
    elif drafts.dry_run:
        report.drafts = f"DRY RUN: {len(drafts.drafted)} drafts not created"
    else:
        count = len(drafts.drafted)
        report.drafts = f"{count} Gmail draft{'' if count == 1 else 's'}"
    return report


def _prepare_safely(desk: Desk, row: JobRow) -> JobReport:
    try:
        return _prepare(desk, row)
    except Exception as exc:  # one job's failure never stops the others
        log.exception("autopilot failed for %s", row.page_id)
        values = (desk.repo.get_values(row.page_id) if desk.repo else None) or {}
        return JobReport(row.page_id, row.company, row.role, row.screen_verdict or "", row.url,
                         resume=f"stopped: {exc}",
                         approved=values.get("Status") in resume_builder.BUILDABLE_STATUSES)


def _finish(desk: Desk, lines: list[str], progress: Progress) -> list[Reply]:
    from jobengine.screen.desk import Reply, short_id

    rows, room = _candidates(desk)
    reports: list[JobReport] = []
    done = 0
    for row in rows:
        if done >= room:
            break
        progress(f"Job {done + 1} of {min(room, len(rows))}: resume, contacts and drafts for "
                 f"{row.company}")
        report = _prepare_safely(desk, row)
        reports.append(report)
        if report.approved:
            done += 1
            _count_approval(desk)
    over = len(rows) - len(reports)
    lines.append(_summary(desk, reports, over))
    replies = [Reply("\n\n".join(t for t in lines if t))]
    for report in reports:
        if report.ask:
            replies.append(Reply(report.ask))
        if report.mail:
            replies.append(Reply(report.mail))
        if report.pdf is not None:
            buttons: list[tuple[str, str]] = []
            if report.resume == "resume saved":
                buttons = [("I applied", f"ia:{short_id(report.job_id)}")]
            replies.append(Reply(f"{report.company}, {report.role}: {report.resume}", buttons,
                                 document=report.pdf))
        replies.extend(Reply(text) for text in report.pack)
    return replies


def _summary(desk: Desk, reports: list[JobReport], over: int) -> str:
    approved = [r for r in reports if r.approved]
    if not approved:
        text = "Autopilot done: no new Apply high or Apply normal job approved."
        if over:
            text += f" Today's limit ({approvals_limit(desk)}) is reached: {over} wait."
        lines = [text, *(r.line(i) for i, r in enumerate(reports, 1))]
        return "\n".join([*lines, "Send /pending to review the other screened jobs."])
    counts: dict[str, int] = {}
    for r in approved:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    split = ", ".join(f"{counts[v]} {v}" for v in AUTO_VERDICTS if counts.get(v))
    out = [f"Autopilot done: {len(approved)} job{'' if len(approved) == 1 else 's'} approved "
           f"({split}).", *(r.line(i) for i, r in enumerate(reports, 1))]
    if over:
        out.append(f"{over} more wait for tomorrow (at most {approvals_limit(desk)} a day).")
    if desk.sending():
        out.append("Mail mode send: mails that passed every check were sent (details below); "
                   "the rest stay in Gmail Drafts. Apply on the job page, then tap I applied "
                   "under the resume. /pending has the rest.")
    else:
        out.append("Drafts are in Gmail and never sent by themselves: read them, send them, "
                   "apply on the job page, then tap I applied under the resume. /pending has "
                   "the rest.")
    return "\n".join(out)
