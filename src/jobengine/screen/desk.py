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
from datetime import date, datetime
from typing import Any

from jobengine import http
from jobengine.bot_state import BotState, FakeBotState
from jobengine.llm import LLMError
from jobengine.notion_repo import FakeJobsRepo, JobsRepo
from jobengine.reference import Reference
from jobengine.screen import jd_capture
from jobengine.screen.jd_capture import Capture, Captures
from jobengine.screen.models import JobRow, ScreenSummary
from jobengine.screen.runner import (
    SECTION_PREFIX,
    ScreenDeps,
    ScreenError,
    find_row,
    job_row,
    pending_rows,
    screen_one,
    screen_pending,
    waiting_for_jd,
)
from jobengine.screen.tiering import rank_key
from jobengine.settings import Settings
from jobengine.sweep.normalize import dedupe_key

log = logging.getLogger("jobengine.screen.desk")

APPROVED_TEXT = "Approved. Resume building arrives in Module 04."
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MATRIX_LINE = re.compile(r"^(Strong|Transferable|Gap) \| ")
MAX_WAITING_LIST = 10
REFERENCE_TTL_SECONDS = 600
NO_TARGET = "DRY RUN: no Job Opportunities target in this environment, nothing to show."


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (label, callback data)


def short_id(page_id: str) -> str:
    """Page ID for callback data: a Notion UUID without hyphens (32 characters)."""
    return page_id.replace("-", "") if UUID_RE.match(page_id) else page_id


def same_page(a: str, b: str) -> bool:
    return short_id(a) == short_id(b)


def on_job_approved(page_id: str) -> str:
    """Hook for Module 04 (resume building). For now it only answers."""
    log.info("job approved: %s", page_id)
    return APPROVED_TEXT


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


class Desk:
    def __init__(
        self,
        s: Settings,
        deps: ScreenDeps,
        state: BotState,
        today: Callable[[], date] = date.today,
        now: Callable[[], datetime] = datetime.now,
    ):
        self.s = s
        self.deps = deps
        self.today = today
        self.now = now
        window = int(s.screening.get("jd_capture_minutes", jd_capture.WINDOW_MINUTES))
        self.captures = Captures(state, window)
        self._reference: tuple[float, Reference] | None = None

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

    def card(self, row: JobRow, index: int, total: int, buttons: bool = True) -> Reply:
        assert self.repo is not None
        counts = match_counts(self.repo.read_body(row.page_id))
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

    def pending(self, index: int = 0) -> list[Reply]:
        if self.repo is None:
            return [Reply(NO_TARGET)]
        rows = self.ranked()
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
        if action not in ("ap", "sk") or not arg:
            return [Reply("That button is no longer valid. Send /pending.")]
        before = self.ranked()
        position = next((i for i, r in enumerate(before) if same_page(r.page_id, arg)), 0)
        values = self.repo.get_values(arg)
        if not values or values.get("Status") != "Screened":
            return [Reply("Already handled"), *self.pending(position)]
        page_id = next((r.page_id for r in before if same_page(r.page_id, arg)), arg)
        if action == "ap":
            self.repo.update(page_id, {"Status": "Approved"})
            first = Reply(on_job_approved(page_id))
        else:
            self.repo.update(page_id, {"Status": "Declined"})
            first = Reply(f"Skipped: {values.get('Company')}, {values.get('Role')}")
        return [first, *self.pending(position)]

    # ------------------------------------------------------------ /screen and /fetch

    def _summary_text(self, summary: ScreenSummary) -> str:
        assert self.repo is not None
        ready = len(pending_rows(self.repo))
        return f"{summary.text()}\n{ready} ready to review: /pending"

    def screen(self, args: str = "", progress: Callable[[str], None] | None = None) -> str:
        if self.repo is None:
            return NO_TARGET
        try:
            if args.strip():
                summary = screen_one(self.s, self.deps, self.today(), args.strip())
            else:
                summary = screen_pending(self.s, self.deps, self.today(), progress)
        except (ScreenError, LLMError, http.HttpError) as exc:
            return f"Screening could not run: {exc}"
        return self._summary_text(summary)

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
            return None
        if self.captures.expired(capture, self.now()):
            return [self._discarded(capture)]
        capture = self.captures.add(capture, message)
        if capture.page_id is None and self._create_row(capture) is None:
            return [Reply("Still need the company and role as two lines:\n"
                          "Company: <name>\nRole: <title>")]
        return [Reply(f"Got it ({capture.chars} characters so far). Send /done when finished.")]

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
    deps.llm = lambda config: FakeLLM(default={})
    clock = now or (lambda: datetime.combine(today, datetime.min.time()).replace(hour=9))
    return Desk(s, deps, FakeBotState(), today=lambda: today, now=clock)


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
    return Desk(s, real_deps(s), state)
