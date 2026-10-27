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
from urllib.parse import urlsplit

from jobengine import alerts, heartbeat, http
from jobengine.apply import answers, autofill, cover, interview
from jobengine.apply import facts as apply_facts
from jobengine.bot_state import BotState, FakeBotState
from jobengine.contacts import connections
from jobengine.contacts import finder as contact_finder
from jobengine.contacts.finder import ContactDeps
from jobengine.llm import LLMError
from jobengine.mail import drafter as mail_drafter
from jobengine.mail import sender as mail_sender
from jobengine.mail import timing as mail_timing
from jobengine.mail.drafter import MailDeps
from jobengine.notion_repo import FakeJobsRepo, JobsRepo, select_filter
from jobengine.reference import Reference
from jobengine.resume import builder as resume_builder
from jobengine.resume.builder import BuildOutcome, ResumeDeps
from jobengine.safety import contact_lookups, paid_api_allowed
from jobengine.screen import (
    add_link,
    autopilot,
    batch,
    jd_capture,
    keywords_report,
    learning,
    waiting,
)
from jobengine.screen.jd_capture import Capture, Captures
from jobengine.screen.models import JobRow, ScreenSummary, split_gaps
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
    unscreened_rows,
    waiting_for_jd,
)
from jobengine.screen.tiering import rank_key
from jobengine.screen.visa import agency_by_name
from jobengine.settings import ROOT_DIR, Settings
from jobengine.strategy import runner as strategy_runner
from jobengine.strategy import tips as tips_mod
from jobengine.strategy.runner import StrategyDeps
from jobengine.sweep.fulltext import blocked_hosts
from jobengine.sweep.normalize import Rules, dedupe_key, place, remote_only, without_region
from jobengine.sweep.rank import Ranker
from jobengine.track import commands as track_commands
from jobengine.track import day_ledger
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
PICK_KEY = "resume.gap_pick"  # bot_state: {"job": <page id>, "gaps": [...], "picked": [...]}
MAX_PICK = 8  # gap names offered in the picker
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
MAX_WAITING_LIST = 25
MIN_PER_BOARD = 5
REFERENCE_TTL_SECONDS = 600
NO_TARGET = "DRY RUN: no Job Opportunities target in this environment, nothing to show."
ADD_USAGE = ("Send /add <job link>: the job's page on the company site (the page LinkedIn or "
             "another app sends you to). The bot reads it, saves and screens the job, adds the "
             "company to Target Companies and builds your resume.")
ADD_PASTE = "Paste the description instead: send /jd {url}, paste the text, then /done."
ADD_TIMEOUT = 30.0  # seconds for the /add page (a /fetch page gets http.PAGE_TIMEOUT_SECONDS)
AUTO_PREP_KEY = "autoprep.done"  # bot_state: {"jobs": {page id: iso date}} (D)
ADD_PLACE_CHARS = 3000  # the start of the job text where /add looks for the place
ADD_LONGER_CHARS = 200  # a /add description this much longer replaces a full one
ADD_BLOCKED = ("The bot never opens {host} pages. Open the job there, tap Apply, and send /add "
               "with the company site's link it takes you to.\nOr " + ADD_PASTE[0].lower() +
               ADD_PASTE[1:])
# The bot sends a long text as several messages (telegram_bot.split_text); this only stops
# a runaway answer.
MAX_TEXT = 20000
NO_GAP = ("none", "n/a", "-")  # a Gaps cell that says there is none
HIGH_ALERT = "\U0001F525 Apply high: apply today while the posting is fresh."
MAX_HIGH_ALERTS = 5
# About what one screened job costs on Haiku (measured 10 Oct), for the /screen choice.
SCREEN_COST_PER_JOB = 0.003
MAX_KEYWORDS = 6  # missing keywords shown on a /pending card
MAX_REWORD = 4  # cards sent at once after a screening; the rest are in /pending


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (label, callback data)
    document: tuple[str, bytes] | None = None  # (filename, PDF bytes); text is the caption
    edit: bool = False  # after a button tap: replace that message instead of sending one
    column: bool = False  # one button per row (a list to pick from)


SCREEN_USAGE = ("/screen screens the jobs waiting to be screened. Other forms:\n"
                "/screen batch - send them to the half-price batch\n"
                "/screen collect - save the batch answers\n"
                "/screen <job link or page id> - screen one job again\n"
                "Jobs ready for your decision are in /pending.")


def _job_ref(ref: str) -> bool:
    """False for a plain word or a sentence: /screen pending (8 Oct) reached Notion as a
    page id and failed with HTTP 400. Links, posting IDs and page ids pass."""
    return " " not in ref and not ref.isalpha()


def _contacts_reply(result: Any) -> Reply:
    """A contact lookup's message; the domain question gets a button per domain found."""
    buttons = []
    if result.status == "waiting_domain":
        ref = contact_finder.job_ref(result.job_id)
        for domain in result.domain_options:
            data = f"dm:{ref}:{domain}"
            if len(data.encode()) <= 64:  # Telegram's limit for button data
                buttons.append((f"Use {domain}", data))
    return Reply(result.message, buttons)


def _one_line(text: str | None) -> str:
    """A company or title on one line (some alert titles carry a line break)."""
    return " ".join((text or "").split())


def _by_board(rows: list[JobRow]) -> str:
    """"12 LinkedIn, 9 Pracuj.pl" for the jobs waiting for a description."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.board or "Other"] = counts.get(row.board or "Other", 0) + 1
    return ", ".join(f"{n} {board}" for board, n in counts.items())


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
    spelled = {g.casefold(): g for g in split_gaps(known)}
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
PENDING_COUNTRY = "pending.country"  # bot_state: {"country": <Country option> | None}
REMOTE_EU = "Other"  # Country option of remote jobs open across the EU or Europe
COUNTRY_ORDER = ("Poland", "Netherlands", "Ireland", REMOTE_EU)
COUNTRY_WORDS = {"pl": "Poland", "poland": "Poland", "polska": "Poland",
                 "nl": "Netherlands", "netherlands": "Netherlands", "holland": "Netherlands",
                 "ie": "Ireland", "ireland": "Ireland",
                 "remote": REMOTE_EU, "eu": REMOTE_EU, "europe": REMOTE_EU, "other": REMOTE_EU}
CHOOSE_COUNTRY = ("Which country do you want to review now? Only that country's jobs come, "
                  "one at a time, so you switch the VPN once.")


def country_label(country: str | None) -> str:
    return "Remote EU" if country in (None, "", REMOTE_EU) else country


def _agency(values: dict[str, Any]) -> bool:
    """A recruitment agency's post (Visa flags): no cold mails (decision of 30 Sep)."""
    from jobengine.screen.visa import AGENCY_FLAGS

    return bool(set(AGENCY_FLAGS) & set(values.get("Visa flags") or []))


REPORT_BUTTONS = 4  # reason buttons under /fetchreport
MAX_SKILL_BUTTONS = 8  # skills offered under /gaps
MAX_SKILL_WORDS = 3  # longer gaps are sentences ("Experience designing CI/CD pipelines")
LINKEDIN_PASTE = ("Open the link, copy the whole job description and paste it here (several "
                  "messages are fine), then tap Done. The bot saves it in Notion, screens the "
                  "job (one AI call) and shows its card for Approve or Skip.")
BUILD_FAILED_HINT = ("The job stays Approved without a resume. Tap Try again to build it again, "
                     "reply with a correction, or tap Not applying to drop it and see the next "
                     "job.")
NO_FULL_JD = ("Only a short snippet of the job description was found, so the match is not "
              "checked. Open the link to read the job, or paste it with /jd <link>.")


def short_link(url: str | None) -> str | None:
    """A LinkedIn alert link without its tracking query (it made the /pending card huge,
    5 Oct test P7); any other link as it is."""
    if url and jd_capture.linkedin_posting_id(url):
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"
    return url


def waits_for_you(reply: Reply) -> bool:
    """A reply whose buttons keep the job on screen (a failed build, Add anyway)."""
    return any(data.startswith(("rb:", "fc:")) for _, data in reply.buttons)


FETCH_HINT = ("After your last job: /fetchcontacts finds the contacts and writes the Gmail "
              "drafts, with the resume attached, for every job you applied to today.")
NOT_APPLYING_ASK = "Why are you not applying? (It goes to Notion.)"
# code -> (what you tapped, the Skip reason option)
NOT_APPLYING = {"c": ("the job is closed or expired", "Expired"),
                "f": ("not a fit after all", "Other"),
                "o": ("another reason", "Other")}


TOKEN_ERRORS = ("invalid_grant", "expired or revoked")
TOKEN_ALERT_KEY = "gmail_token_alert"
TOKEN_ALERT = ("\u26a0\ufe0f A Gmail token has expired (invalid_grant): replies and sent mails "
               "are not checked. /gmailtoken shows which one; /gmailtoken m02 or /gmailtoken "
               "mm renews it in 2 minutes (a sign-in link, then you paste the address you land "
               "on), then /restart. While the Google OAuth app is in Testing, tokens expire "
               "every 7 days: publishing the app stops that (Phase 9, P9-37).")


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
        # /gaps "Hands-on" / "Production" buttons: adds a Skills Inventory row (real_desk and
        # fake_desk set it; None means the buttons are not offered).
        self.add_skill: Callable[[str, str, str], None] | None = None
        # 🤝 LinkedIn Connections (contacts/connections.py): None when not set up here.
        self.connections: connections.ConnectionsStore | None = None
        # /add (23 Oct): adds a Target Companies row (notion_repo.CompanyAdder); None: not here.
        self.companies: Any = None
        # The daily report and failure alerts by email (alerts.py): (subject, text) -> None
        # when sent, else why not.
        self.owner_mail: alerts.Mailer = lambda subject, text: alerts.owner_mail(
            self.s, subject, text)
        # Auto-apply (H8): the Notion "Answer bank" and "Auto-apply issues" tables
        # (notion_repo.NotionTable); None where not writable (DRY RUN).
        self.answer_bank: Any = None
        self.autoapply_issues: Any = None
        # The outside heartbeat (heartbeat.py, H4): True pings the check, False its /fail.
        self.heartbeat: Callable[[bool], bool] = lambda ok: heartbeat.ping(self.s, ok)
        # Set by the bot for each update: sends a reply at once (before a long build).
        self.notify: Callable[[Reply], None] | None = None
        self.today = today
        self.now = now
        window = int(s.screening.get("jd_capture_minutes", jd_capture.WINDOW_MINUTES))
        self.captures = Captures(state, window)
        self._reference: tuple[float, Reference] | None = None
        self._ranker: Ranker | None = None
        # The Irish permits list (ie_permits.py): read once a day, None when it failed; the
        # note says where it came from (for /health).
        self._permits: tuple[date, Any] | None = None
        self.permits_note = ""
        self.out_dir = ROOT_DIR / "out"
        self.get_file: Callable[[str], tuple[str, str, bytes]] = \
            lambda url: http.get_file(url, s=self.s)
        # Reads one public page (the employer's link behind an Adzuna job).
        self.get_page: Callable[[str], tuple[str, str]] = \
            lambda url: http.get_page(url, s=self.s, blocked=blocked_hosts(self.s))
        # /add (B-03, 26 Oct): you wait for that one page, so a slow company site gets longer.
        self.get_slow_page: Callable[[str], tuple[str, str]] = \
            lambda url: http.get_page(url, s=self.s, blocked=blocked_hosts(self.s),
                                      timeout=ADD_TIMEOUT)

    @property
    def repo(self) -> JobsRepo | None:
        return self.deps.repo

    def reference(self) -> Reference:
        stamp = self.now().timestamp()
        if self._reference is None or stamp - self._reference[0] > REFERENCE_TTL_SECONDS:
            self._reference = (stamp, self.deps.reference())
        return self._reference[1]

    # ------------------------------------------------------------ /pending

    def permits(self) -> Any:
        """The Irish permits list (flow feature 6), read once a day; None when unavailable."""
        from jobengine import ie_permits

        today = self.today()
        if self._permits is None or self._permits[0] != today:
            url = (self.deps.config().get(ie_permits.URL_KEY)
                   or self.s.screening.get("ie_permits_url"))
            found, problem = None, "no gov.ie link in Config ie_permits.url"
            if url:
                try:
                    found = ie_permits.load(str(url), self.get_file)
                    self.permits_note = f"read from gov.ie ({len(found.counts)} companies)"
                except (http.HttpError, ValueError, OSError) as exc:
                    log.warning("Irish permits list not read: %s", exc)
                    problem = f"gov.ie: {str(exc)[:120]}"
                except Exception as exc:  # a broken file never stops /pending
                    log.exception("Irish permits list not read")
                    problem = f"gov.ie: {type(exc).__name__}"
            if found is None:  # P9-20: gov.ie refuses the bot; the copy you saved instead
                saved = ie_permits.local_file(self.out_dir)
                try:
                    if saved is None:
                        raise FileNotFoundError("out/ie_permits.xlsx missing")
                    found = ie_permits.load_file(saved)
                    self.permits_note = (f"read from out/{saved.name} ({len(found.counts)} "
                                         f"companies; {problem})")
                except Exception as exc:  # a broken file never stops /pending
                    log.warning("Irish permits file not read: %s", exc)
                    self.permits_note = (f"NOT read ({problem}; {exc}). To fix: "
                                         f"{ie_permits.DOWNLOAD_HINT}")
            self._permits = (today, found)
        return self._permits[1]

    def irish_permits(self, row: JobRow) -> tuple[str, int] | None:
        if row.country != "Ireland":
            return None
        permits = self.permits()
        return permits.match(row.company) if permits is not None else None

    def all_ranked(self) -> list[JobRow]:
        assert self.repo is not None
        ref, today = self.reference(), self.today()
        rows = pending_rows(self.repo)

        def key(row: JobRow) -> tuple[int, int, int]:
            tier, top, score = rank_key(row, ref, today)
            # A company that got Irish permits last year likely sponsors again (feature 6).
            return tier, top, score + (2 if self.irish_permits(row) else 0)

        return sorted(rows, key=key, reverse=True)

    def chosen_country(self) -> str | None:
        """The country /pending shows (None: all), chosen with the country buttons."""
        value = self.state.get(PENDING_COUNTRY) or {}
        return value.get("country")

    def choose_country(self, country: str | None) -> None:
        self.state.set(PENDING_COUNTRY, {"country": country})

    def ranked(self) -> list[JobRow]:
        """The jobs to review, best first, only the chosen country's when one is chosen."""
        rows = self.all_ranked()
        country = self.chosen_country()
        if not country:
            return rows
        return [r for r in rows if (r.country or REMOTE_EU) == country]

    def country_menu(self, rows: list[JobRow] | None = None, text: str = CHOOSE_COUNTRY) -> Reply:
        rows = self.all_ranked() if rows is None else rows
        counts: dict[str, int] = {}
        for row in rows:
            key = row.country or REMOTE_EU
            counts[key] = counts.get(key, 0) + 1
        order = [c for c in COUNTRY_ORDER if c in counts] + sorted(
            c for c in counts if c not in COUNTRY_ORDER)
        buttons = [(f"{country_label(c)} ({counts[c]})", f"pc:{c}") for c in order]
        buttons.append((f"All ({len(rows)})", "pc:all"))
        linkedin = len(self.linkedin_rows()) if self.repo is not None else 0
        if linkedin:  # LinkedIn jobs wait for their description before they can be reviewed
            buttons.append((f"LinkedIn ({linkedin})", "li:next:0"))
        return Reply(text, buttons)

    def pending_command(self, args: str = "") -> list[Reply]:
        """/pending: the country buttons when the jobs are in more than one country, else the
        first card. /pending <country> (pl, nl, ie, remote, all) goes straight to it."""
        if self.repo is None:
            return [Reply(NO_TARGET)]
        word = args.strip().lower()
        if word:
            if word == "all":
                self.choose_country(None)
            elif word in COUNTRY_WORDS:
                self.choose_country(COUNTRY_WORDS[word])
            else:
                return [Reply("Send /pending, or /pending poland, netherlands, ireland, remote "
                              "or all.")]
            return self.pending(0)
        rows = self.all_ranked()
        if len({r.country or REMOTE_EU for r in rows}) <= 1:
            self.choose_country(None)
            return self.pending(0, rows)
        return [self.country_menu(rows)]

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

    def match_lines(self, row: JobRow, body: list[str], counts: dict[str, int]) -> list[str]:
        """The match lines of a card. With no full description nothing was matched, so the
        card says so instead of showing an empty match as "Gaps: none"."""
        _jd, kind = description(body)
        if kind != "full" and not any(counts.values()) and not row.gaps:
            return [NO_FULL_JD]
        return [f"Match: {counts['Strong']} strong, {counts['Transferable']} transferable, "
                f"{counts['Gap']} gaps", *self.keyword_lines(body),
                f"Gaps: {'; '.join(split_gaps(row.gaps)) or 'none'}"]

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
            *self.match_lines(row, body, counts),
        ]
        permits = self.irish_permits(row)
        if permits:
            lines.append(f"Irish permits: {permits[1]} issued to {permits[0]} (DETE list)")
        if row.url:
            lines.append(short_link(row.url) or row.url)
        keys = []
        if buttons:
            pid = short_id(row.page_id)
            keys = [("Approve", f"ap:{pid}"), ("Skip", f"sk:{pid}"), ("Next", f"nx:{index + 1}")]
            if self.chosen_country():
                keys.append(("Change country", "pc:menu"))
        return Reply("\n".join(lines), keys)

    def waiting_line(self) -> str | None:
        assert self.repo is not None
        rows = waiting_for_jd(self.repo, self.state)
        if not rows:
            return None
        need = "jobs need" if len(rows) > 1 else "job needs"
        return f"{len(rows)} {need} the JD ({_by_board(rows)}): /jd"

    def pending(self, index: int = 0, rows: list[JobRow] | None = None) -> list[Reply]:
        """The card at `index`. `rows` reuses a ranking already read (a button tap)."""
        if self.repo is None:
            return [Reply(NO_TARGET)]
        rows = self.ranked() if rows is None else rows
        waiting = self.waiting_line()
        if not rows:
            country = self.chosen_country()
            others = self.all_ranked() if country else []
            if others:
                return [self.country_menu(others, f"No more jobs for {country_label(country)}. "
                                                  "Pick the next country:")]
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
        if action == "pc" and arg:  # a country button of /pending
            if arg == "menu":
                return [self.country_menu()]
            self.choose_country(None if arg == "all" else arg)
            return self.pending(0)
        if action in ("ra", "rb", "cl", "rq", "ia", "na", "nr", "fg", "fc", "gp", "gt", "gr",
                      "gx") and arg:
            return self.resume_tap(action, arg)
        if action in ("rc", "lk", "rd") and arg:
            return self.track_tap(action, data)
        if action in ("sa", "sr", "sc", "sx") and arg:
            return self.strategy_tap(action, arg)
        if action == "oc" and arg:
            return self.outreach_tap(arg)
        if action == "ln" and arg:  # "LinkedIn notes" under the contacts
            return self.linkedin_notes(arg)
        if action in ("en", "nj", "sz", "dd") and arg:  # the apply nudge's buttons (H7)
            return self.nudge_tap(action, arg)
        if action == "aa" and arg:  # /add of a job screening skipped: "Build the resume anyway"
            return self.add_anyway(arg)
        if action == "lp" and arg in ("yes", "no"):  # "Is your laptop on?" (auto-apply queue)
            return self.laptop_tap(arg)
        if action in ("fo", "fw") and arg:  # under a dry run: Answers OK, Something is wrong
            return self.dryrun_tap(action, arg)
        if action in ("sf", "sw") and arg:  # the spot check's buttons (H5)
            return self.spot_tap(action, arg)
        if action in ("dc", "dn") and arg:  # /newcompanies: Add, Not interested
            return self.newcompany_tap(action, arg)
        if action == "tu" and arg:  # "Undo" under a company /add put in Target Companies
            return self.add_undo(arg)
        if action == "rf" and arg:  # "Referral asks": your LinkedIn connections there
            return self.referral_tap(arg)
        if action == "rm" and arg:  # "Mail them": the referral ask to connections by mail
            return self.referral_mail_tap(arg)
        if action == "rl" and arg:  # "Location and email": Prospeo for those connections
            return self.connection_lookup_tap(arg)
        if action == "dm" and arg:  # a domain button under the domain question
            return self.domain_tap(arg)
        if action == "fr" and arg:  # a reason button under /fetchreport
            return self.fetchreport_command(arg)
        if action == "li" and arg:  # the LinkedIn queue (/linkedin)
            return self.linkedin_tap(arg)
        if action == "gs" and arg:  # a skill button under /gaps
            return self.skill_tap(arg)
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
        if action not in ("ap", "sk", "ab", "xe") or not arg:
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
        if action == "xe":  # "Skip as expired" under the closed-posting warning
            self.repo.update(page_id, {"Status": "Expired"})
            return [Reply(f"Marked expired: {job}"), *self.pending(position, rest)]
        if action == "ap":
            from jobengine.apply import link

            closed = link.closed_reason(values.get("URL") or "", self.get_page, self.today())
            if closed:  # Phase 6: no resume for a posting that is gone, unless you say so
                pid = short_id(page_id)
                return [Reply(f"\u26A0\uFE0F {job}: the posting looks closed ({closed}).\n"
                              f"{values.get('URL')}\nBuild the resume anyway?",
                              [("Build anyway", f"ab:{pid}"),
                               ("Skip as expired", f"xe:{pid}")])]
        if action in ("ap", "ab"):
            self.repo.update(page_id, {"Status": "Approved"})
            self.say(Reply(APPROVED_TEXT.format(job=job)))
            return self.after_build(self.on_job_approved(page_id, values), position, rest)
        self.repo.update(page_id, {"Status": "Declined"})
        return [Reply(f"Skipped: {job}"), *self.pending(position, rest)]

    def after_build(self, replies: list[Reply], position: int = 0,
                    rest: list[JobRow] | None = None) -> list[Reply]:
        """One job at a time: while its resume waits for you (Approve resume or Rebuild) and
        then for I applied or Not applying, the next job is not shown. A build that failed
        waits too (Try again or Not applying); only a job with no resume to build at all
        lets the next job come at once."""
        if self.resume is None or not any(r.document or waits_for_you(r) for r in replies):
            return [*replies, *self.pending(position, rest)]
        if not any(r.document for r in replies):
            return replies  # a failed build: its buttons say what comes next
        return [*replies, Reply(ONE_AT_A_TIME)]

    # ------------------------------------------------------------ resumes (Module 04)

    def say(self, reply: Reply) -> None:
        if self.notify is not None:
            self.notify(reply)

    def on_job_approved(
        self, page_id: str, values: dict[str, Any], *, correction: str | None = None,
        force: bool = False, force_terms: list[str] | None = None, claude: bool = False,
    ) -> list[Reply]:
        """Build (or show) the resume for an approved job. `claude`: the "Rebuild with
        Claude" button (the resume model is Gemini)."""
        if self.resume is None:
            return [Reply(NO_RESUME)]
        who = " with Claude" if claude else ""
        self.say(Reply(f"Building resume for {values.get('Company')}{who}..."))
        deps = resume_builder.with_claude(self.resume) if claude else self.resume
        outcome = resume_builder.build_resume(
            deps, page_id, correction=correction, force=force, force_terms=force_terms)
        if outcome.status == "correction_refused" and correction:
            # "Add anyway": list it in the skills section; you learn it before the interview.
            self.state.set(FORCE_KEY, {"job": page_id, "text": correction})
            return [Reply(outcome.message,
                          [("Add anyway (I'll learn it)", f"fc:{short_id(page_id)}")])]
        if outcome.status in ("failed", "correction_refused"):
            # The job is Approved but has no resume: keep it on screen until you decide.
            job = short_id(page_id)
            return [Reply(f"{outcome.message}\n{BUILD_FAILED_HINT}",
                          [("Try again", f"rb:{job}"), ("Not applying", f"na:{job}")])]
        return [self.preview(outcome)]

    def preview(self, outcome: BuildOutcome) -> Reply:
        if not outcome.ok or outcome.pdf is None:
            return Reply(outcome.message)
        name = outcome.filename or "resume.pdf"
        buttons = [("Rebuild", f"rb:{short_id(outcome.job_id)}")]
        if outcome.log_id:
            log = outcome.log_id.replace("-", "")
            buttons = [("Approve resume", f"ra:{log}"), ("Rebuild", f"rq:{log}")]
        if outcome.model and not outcome.model.startswith("claude-"):
            # DeepSeek or Gemini wrote it: one tap gets the same job written by Claude.
            buttons.append(("Rebuild with Claude", f"cl:{short_id(outcome.job_id)}"))
        plan = outcome.plan
        forced = {f.casefold() for f in (plan.forced_skills if plan else [])}
        gaps = [g for g in (plan.gaps_reported if plan else []) if g.casefold() not in forced]
        if len(gaps) > 1:  # FR-1 (P9-20): pick several, one rebuild
            buttons.append(("Add skills", f"gp:{short_id(outcome.job_id)}"))
        for gap in gaps[:MAX_GAP_BUTTONS if len(gaps) == 1 else 0]:
            data = f"fg:{short_id(outcome.job_id)}:{gap}"
            if len(data.encode()) <= 64:  # Telegram's limit for button data
                buttons.append((f"Add {gap}", data))
        text = outcome.caption or ""
        line = self.resume_cover_line(outcome)
        if line and len(text) + len(line) + 1 <= resume_builder.CAPTION_MAX:
            first, _, rest = text.partition("\n")
            text = f"{first}\n{line}\n{rest}" if rest else f"{first}\n{line}"
        return Reply(text, buttons, document=(name, outcome.pdf))

    def _pick_gaps(self, values: dict[str, Any]) -> list[str]:
        """The job's gaps that are skill names (not sentences) and not yours yet."""
        ref = self.reference()
        return [g for g in split_gaps(values.get("Gaps"))
                if len(g.split()) <= MAX_SKILL_WORDS and ref.lookup(g) is None][:MAX_PICK]

    def _picker(self, pick: dict[str, Any], edit: bool) -> Reply:
        job = short_id(str(pick["job"]))
        picked = set(pick.get("picked") or [])
        buttons = [(f"{'☑' if i in picked else '☐'} {gap}", f"gt:{job}:{i}")
                   for i, gap in enumerate(pick["gaps"])]
        if picked:
            buttons.append((f"Rebuild with {len(picked)} skill{'s' if len(picked) > 1 else ''}",
                            f"gr:{job}"))
        buttons.append(("Cancel", f"gx:{job}"))
        names = [pick["gaps"][i] for i in sorted(picked)]
        text = ("Tap the skills you really have (each tap ticks or unticks one), then Rebuild: "
                "one new resume with all of them, and they are added to Skills Inventory as "
                "Hands-on (your choice of 23 Oct)."
                + (f"\nPicked: {', '.join(names)}" if names else ""))
        return Reply(text, buttons, edit=edit, column=True)

    def gap_pick_tap(self, action: str, arg: str) -> list[Reply]:
        """FR-1 (Phase 9 P9-20): "Add skills" under a preview lists the job's gaps; you tick
        several and one rebuild adds them all (and saves them as Hands-on skills)."""
        from jobengine.resume.builder import notion_id

        job_arg, _, index = arg.partition(":")
        job_id = notion_id(job_arg)
        if action == "gp":
            values = (self.repo.get_values(job_id) if self.repo else None) or {}
            gaps = self._pick_gaps(values)
            if not gaps:
                return [Reply("No missing skill to pick for this job (the gaps are sentences, "
                              "or already your skills).")]
            pick = {"job": job_id, "gaps": gaps, "picked": []}
            self.state.set(PICK_KEY, pick)
            return [self._picker(pick, edit=False)]
        pick = self.state.get(PICK_KEY) or {}
        if not pick.get("gaps") or not same_page(str(pick.get("job")), job_id):
            return [Reply("That list is no longer open. Tap Add skills under the preview again.")]
        if action == "gx":
            self.state.delete(PICK_KEY)
            return [Reply("Nothing added.", edit=True)]
        if action == "gt":
            picked = set(pick.get("picked") or [])
            if index.isdigit() and int(index) < len(pick["gaps"]):
                picked ^= {int(index)}
            pick["picked"] = sorted(picked)
            self.state.set(PICK_KEY, pick)
            return [self._picker(pick, edit=True)]
        terms = [pick["gaps"][i] for i in pick.get("picked") or [] if i < len(pick["gaps"])]
        if not terms:
            return [self._picker(pick, edit=True)]
        self.state.delete(PICK_KEY)
        saved, failed = [], []
        if self.add_skill is not None:
            ref = self.reference()
            for term in terms:
                if ref.lookup(term) is not None:
                    continue
                try:
                    self.add_skill(term, "Hands-on",
                                   f"Added from a resume preview on {self.today().isoformat()}.")
                    saved.append(term)
                except http.HttpError as exc:
                    failed.append(f"{term} ({exc})")
            self._reference = None
        note = f"Rebuilding with {', '.join(terms)}."
        if saved:
            note += f" Added to Skills Inventory as Hands-on: {', '.join(saved)}."
        if failed:
            note += f" Not added to Skills Inventory: {'; '.join(failed)}."
        values = (self.repo.get_values(job_id) if self.repo else None) or {}
        rebuilt = self.on_job_approved(job_id, values,
                                       correction=f"add {', '.join(terms)} to the skills",
                                       force=True, force_terms=terms)
        return [Reply(note, edit=True), *rebuilt]

    def resume_cover_line(self, outcome: BuildOutcome) -> str | None:
        """Feature 11: how many of the job's keywords you have the final resume shows."""
        if self.repo is None or not outcome.pdf:
            return None
        jd, _kind = description(self.repo.read_body(outcome.job_id))
        if not jd:
            return None
        terms = list(self.ranker().keywords(jd).have)
        resume_text = cover.pdf_text(outcome.pdf)
        if terms and resume_text:
            day_ledger.add_match(len(cover.cover(terms, resume_text)[0]), len(terms))
        return cover.cover_line(terms, resume_text)

    def resume_tap(self, action: str, arg: str) -> list[Reply]:
        if self.resume is None:
            return [Reply(NO_RESUME)]
        if action in ("rb", "cl"):
            self.state.delete(REBUILD_KEY)
            values = (self.repo.get_values(arg) if self.repo else None) or {}
            return self.on_job_approved(arg, values, force=True, claude=action == "cl")
        if action in ("gp", "gt", "gr", "gx"):  # FR-1: pick several gaps, rebuild once
            return self.gap_pick_tap(action, arg)
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
            done = resume_builder.mark_applied(self.resume, arg)
            if self.nudge_on():
                return [Reply(done), *self.nudge_applied()]
            return [Reply(done), *self.next_job()]
        if action == "na":  # "Not applying": why?
            return [Reply(NOT_APPLYING_ASK, [(label, f"nr:{arg}:{code}")
                                             for code, (label, _) in NOT_APPLYING.items()])]
        if action == "nr":
            job_id, _, code = arg.partition(":")
            reason = self.not_applying(job_id, code)
            if self.nudge_on():
                return [Reply(reason), *self.nudge_card()]
            return [Reply(reason), *self.next_job()]
        result = resume_builder.finalise(self.resume, arg)
        if result.status == "newer":
            latest = [self.preview(result.latest)] if result.latest else []
            return [Reply(result.message), *latest]
        if result.status == "approved" and result.job_id and result.already:
            # A second tap (Phase 13 B-03): the Apply pack and drafts are not made again.
            job = short_id(result.job_id)
            return [Reply("Already approved: the Apply pack and the link are above.",
                          [("I applied", f"ia:{job}"), ("Not applying", f"na:{job}")])]
        if result.status == "approved" and result.job_id:
            # Nothing runs by itself: the next steps are buttons.
            job = short_id(result.job_id)
            buttons = [("I applied", f"ia:{job}"), ("Not applying", f"na:{job}")]
            log.info("approve resume: finding the apply link for %s", result.job_id)
            text = self.apply_link_text(result.job_id, result.job_url, result.message)
            log.info("approve resume: writing the apply pack for %s", result.job_id)
            text += ("\nWhen you are done, tap I applied (or Not applying): then the next job "
                     "comes.")
            if self.contacts is not None and self.mail is not None:
                text += f"\n{FETCH_HINT}"
            return [*self.apply_pack(result.job_id, result.pdf), Reply(text, buttons)]
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

    def apply_pack(self, job_id: str, resume_pdf: bytes | None = None) -> list[Reply]:
        """Ready answers for this job's portal form; also saved on the job's Notion page.
        With the approved resume PDF, a cover letter written from it follows (feature 4)."""
        from jobengine.apply import pack
        from jobengine.resume.builder import specific_details

        if self.repo is None:
            return []
        values = self.repo.get_values(job_id)
        if not values:
            return []
        body = self.repo.read_body(job_id)
        text = pack.build(self.s, self.deps.config(), values, specific_details(body),
                          self.today(), answers.pack_lines(self.state))
        letter = self.cover_letter(job_id, values, body, resume_pdf)
        if self.deps.write:
            try:
                self.repo.append_body(job_id, pack.blocks(text))
                if letter is not None and letter.text:
                    self.repo.append_body(job_id, pack.blocks(cover.block(letter)))
            except http.HttpError as exc:  # the answers still reach you in Telegram
                log.warning("could not save the apply pack on %s: %s", job_id, exc)
        text += tips_mod.lines("Tips for this application",
                               self.tips(tips_mod.APPLICATION, values.get("Country")))
        replies = [Reply(text[:MAX_TEXT])]
        if letter is not None:
            replies.append(Reply(cover.block(letter)[:MAX_TEXT]))
        why = pack.why_line(self.s, self.deps.config(), specific_details(body),
                            split_gaps(values.get("Gaps")))
        replies.append(Reply(autofill.message(self.s, self.deps.config(), self.state, values,
                                              letter=letter.text if letter else None, why=why)))
        return replies

    def autofill_command(self) -> list[Reply]:
        """/autofill: how to add the form autofill bookmark, then the bookmark (feature 9)."""
        return [Reply(text) for text in autofill.setup_text()]

    def dryrun_command(self, args: str) -> list[Reply]:
        """/dryrun <job> [form link]: fill the job's form in a browser, never submit (H8)."""
        parts = args.split()
        if not parts:
            return [Reply("Send /dryrun <job URL or page id>, or /dryrun <job> <form link> when "
                          "the form is on another page. It fills the form and never submits.")]
        return self.dry_run_job(parts[0], parts[1] if len(parts) > 1 else None)

    def dry_run_job(self, ref: str, form_url: str | None = None) -> list[Reply]:
        """One dry run: the form filled from your answers, the AI and the approved resume,
        a screenshot, the report with Answers OK / Something is wrong. Never submitted."""
        from jobengine.apply import form_bot, form_review, pack
        from jobengine.resume.builder import specific_details

        values: dict[str, Any] = {}
        why = None
        row = find_row(self.repo, ref) if self.repo is not None else None
        if row is not None:
            values = self.repo.get_values(row.page_id) or {}
            why = pack.why_line(self.s, self.deps.config(),
                                specific_details(self.repo.read_body(row.page_id)),
                                split_gaps(values.get("Gaps")))
        url = form_url or values.get("URL") or ref
        if "://" not in url:
            return [Reply(f"No Job Opportunities row found for {ref}")]
        job = f"{values.get('Company') or ''}, {values.get('Role') or ''}".strip(", ")
        config = self.deps.config()
        profile = autofill.fields(self.s, config, values, why=why)
        # Your saved answers win over an Answer bank row of the same question (newer).
        saved = [*form_review.bank_answers(self.answer_bank), *answers.items(self.state)]
        body = self.repo.read_body(row.page_id) if row is not None and self.repo else []
        now = self.tracking_now()
        form_review.prune_approved(self.state, now)
        result = form_bot.dry_run(self.s, url, profile, saved, name=job or "form",
                                  ai=self._form_ai(values, body, profile, saved),
                                  resume=self._approved_resume(row.page_id) if row else None)
        replies = []
        if result.screenshot is not None:
            replies.append(Reply(f"Dry run screenshot{f': {job}' if job else ''} (not sent)",
                                 document=(result.screenshot.name,
                                           result.screenshot.read_bytes())))
        buttons: list[tuple[str, str]] = []
        if not result.stopped:
            run_id = form_review.record(self.state, job=job, job_id=row.page_id if row else "",
                                        url=url, shot=result.screenshot,
                                        answers=result.answers, now=now)
            buttons = form_review.buttons(run_id)
        replies.append(Reply(form_bot.report(result, job)[:MAX_TEXT], buttons))
        return replies

    def _approved_resume(self, job_id: str) -> tuple[str, bytes] | None:
        """The job's approved resume PDF (file name, bytes), or None."""
        from jobengine.mail.drafter import resume_name, resume_pdf

        log_repo = self.resume.resume_log if self.resume is not None else None
        if log_repo is None or self.mail is None:
            return None
        rows = [row for _, row in log_repo.rows_for_job(job_id) if row.get("Approved")]
        if not rows:
            return None
        row = max(rows, key=lambda r: r.get("Revision") or 0)
        try:
            return resume_name(dict(row)), resume_pdf(self.mail, dict(row))
        except Exception as exc:  # the dry run still fills the rest
            log.info("dry run: no resume PDF for %s: %s", job_id, exc)
            return None

    # ------------------------------------------------------------ auto-apply queue (H8)

    def autoapply_command(self, args: str) -> list[Reply]:
        """/autoapply: queue the jobs whose resume is built for a dry run (30 a day);
        /autoapply status: the queue and the auto-submit gate."""
        from jobengine.apply import queue

        now = self.tracking_now()
        config = self.deps.config()
        if args.strip().lower() != "status":
            added, left = queue.add(self.state, config, self._ready(), now)
            head = (f"Queued {added} job(s) for a dry run (filled, never submitted); "
                    f"{left} more allowed today.")
        else:
            head = "Auto-apply queue:"
        waiting = len(queue.waiting(self.state, now))
        where = ("the bot on this machine fills them, 3 at a time" if self.s.bot_mode != "webhook"
                 else "your laptop if it is on, else Cloud Run, 3 at a time")
        return [Reply(f"{head}\n{waiting} waiting: {where}. Each result comes with Answers OK "
                      f"and Something is wrong.\n{self._submit_status()}")]

    def _submit_status(self) -> str:
        from jobengine.apply import form_review, queue

        runs = (self.state.get(form_review.KEY) or {}).get("items") or []
        wrong = sum(1 for r in runs[-10:] if r.get("status") == "wrong")
        return queue.submit_status(self.deps.config(), form_review.approved_count(self.state),
                                   wrong)

    def autoapply_tick(self) -> list[Reply]:
        """Every few minutes: fill up to 3 queued forms here, or ask where (Cloud Run)."""
        from jobengine.apply import queue

        now = self.tracking_now()
        config = self.deps.config()
        if (config.get("apply.auto") or "false").strip().lower() == "true":
            queue.add(self.state, config, self._ready(), now)
        elif not queue.waiting(self.state, now):
            return []  # auto-apply off and nothing queued by /autoapply: no Bot State writes
        if self.s.bot_mode != "webhook":  # the bot on your laptop is the laptop worker
            queue.laptop_seen(self.state, now)
            return self._fill_queued("laptop", now)
        decision = queue.cloud_decision(self.state, now)
        if decision == "ask":
            queue.asked(self.state, now)
            count = len(queue.waiting(self.state, now))
            return [Reply(f"💻 Is your laptop on? {count} job(s) wait for a dry run. Yes: your "
                          "laptop fills them (free). No, or no answer in 15 minutes: Cloud "
                          "Run fills them.", [("Yes", "lp:yes"), ("No", "lp:no")])]
        if decision == "cloud":
            return self._fill_queued("cloud", now)
        return []

    def _fill_queued(self, by: str, now: datetime) -> list[Reply]:
        from jobengine.apply import queue

        jobs = queue.take(self.state, by, now)
        replies: list[Reply] = []
        for job, got in zip(jobs, queue.run_parallel(jobs, self.dry_run_job), strict=True):
            queue.finish(self.state, job)
            if isinstance(got, Exception):
                replies.append(Reply(f"Dry run of {job} failed: {got}"))
            else:
                replies.extend(got)
        return replies

    def laptop_tap(self, arg: str) -> list[Reply]:
        from jobengine.apply import queue

        queue.answer(self.state, arg == "yes")
        if arg == "yes":
            return [Reply("OK: your laptop fills them. Keep the bot running there; if it has not "
                          "started in 15 minutes, Cloud Run does it.")]
        return [Reply("OK: Cloud Run fills them now (3 at a time).")]

    def dryrun_tap(self, action: str, run_id: str) -> list[Reply]:
        """Answers OK / Something is wrong under a dry run (H8 step 3)."""
        from jobengine.apply import form_review

        if action == "fo":
            return [Reply(form_review.approve(self.state, run_id, self.tracking_now(),
                                              self.answer_bank))]
        return [Reply(form_review.wrong_question(self.state, run_id)[:MAX_TEXT])]

    def dryrun_note(self, replied_to: str, text: str) -> list[Reply] | None:
        """A reply to "What is wrong in ...? Ref DRY-...": an issue row, the answer saved."""
        from jobengine.apply import form_review

        if form_review.REF not in replied_to:
            return None
        saved = form_review.correct(self.state, replied_to, text, self.tracking_now().date(),
                                    self.answer_bank, self.autoapply_issues)
        return [Reply(saved)] if saved else None

    def _form_ai(self, values: dict[str, Any], body: list[str], profile: dict[str, str],
                 saved: list[dict[str, str]]) -> Callable[[list[Any]], int]:
        """The AI step of the dry run (apply/form_ai.py): your facts, voice and skills."""
        from jobengine.apply import facts as facts_mod
        from jobengine.apply import form_ai

        config = self.deps.config()
        try:
            skills = form_ai.skills_lines(self.reference().skills)
        except Exception as exc:  # the AI then has the resume only
            log.info("dry run: no Skills Inventory: %s", exc)
            skills = []
        jd, _kind = description(body)
        ctx = form_ai.Context(facts=facts_mod.facts(self.s, config), profile=profile,
                              saved=saved, skills=skills, resume=self._master_text(),
                              company=values.get("Company") or "",
                              role=values.get("Role") or "", jd=jd or "")
        gaps = split_gaps(values.get("Gaps"))
        return lambda found: form_ai.fill(self.deps.llm(config), found, ctx, gaps)

    def answer_command(self, args: str) -> list[Reply]:
        """/answer <question> = <answer>: kept for every later Apply pack (feature 10)."""
        return [Reply(answers.command(self.state, args))]

    def answers_command(self) -> list[Reply]:
        """/answers: your saved form answers."""
        return [Reply(answers.listing(self.state)[:MAX_TEXT])]

    def cover_letter(self, job_id: str, values: dict[str, Any], body: list[str],
                     resume_pdf: bytes | None) -> cover.Letter | None:
        """Feature 4: None when there is no approved PDF to write from (/applypack)."""
        from jobengine.resume.builder import specific_details

        if resume_pdf is None or self.resume is None:
            return None
        jd, _kind = description(body)
        resume_text = cover.pdf_text(resume_pdf)
        if not resume_text.strip():  # nothing to write from (a PDF that cannot be read)
            log.info("approve resume: no cover letter, the PDF text is empty (%s)", job_id)
            return None
        log.info("approve resume: writing the cover letter for %s", job_id)
        return cover.write(
            self.resume.llm(self.deps.config()),
            company=without_region(values.get("Company") or ""), role=values.get("Role") or "",
            country=values.get("Country") or "", jd=jd,
            resume_text=resume_text, details=specific_details(body),
            gaps=split_gaps(values.get("Gaps")), key=job_id.replace("-", ""),
            facts=apply_facts.facts(self.s, self.deps.config()))

    def tips(self, categories: tuple[str, ...], country: str | None) -> list[str]:
        """Adopted strategy tips of these categories for this country (strategy/tips.py);
        none when the Strategy table cannot be read."""
        if self.strategy is None:
            return []
        try:
            return tips_mod.pick(tips_mod.adopted(self.strategy.existing()), categories,
                                 country)
        except Exception as exc:  # tips are extra: never fail a reply over them
            log.warning("strategy tips not read: %s", exc)
            return []

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

    # ------------------------------------------------------------ interviews (feature 15)

    def _interview_job(self, ref: str, usage: str) -> tuple[str, dict[str, Any]] | Reply:
        if self.repo is None:
            return Reply(NO_TARGET)
        if not ref:
            return Reply(usage)
        row = find_row(self.repo, ref)
        if row is None:
            return Reply(f"No Job Opportunities row found for {ref}")
        return row.page_id, self.repo.get_values(row.page_id) or {}

    def _master_text(self) -> str:
        """Your Golden Master resume as plain text ("" when it cannot be read)."""
        from jobengine.resume.master import load_master, normalised_text

        if self.resume is None:
            return ""
        try:
            return normalised_text(load_master(self.resume.blocks(), self.deps.config()).html)
        except Exception as exc:  # the prep pack still has everything else
            log.info("interview prep: Golden Master not read: %s", exc)
            return ""

    def prep_command(self, args: str) -> list[Reply]:
        """/prep <job>: the interview prep pack (feature 15)."""
        from jobengine.apply.pack import names_gap
        from jobengine.resume import header
        from jobengine.resume.builder import specific_details

        found = self._interview_job(args.strip(), "Send /prep <job URL or page id>.")
        if isinstance(found, Reply):
            return [found]
        job_id, values = found
        body = self.repo.read_body(job_id) if self.repo else []
        jd, _kind = description(body)
        gaps = split_gaps(values.get("Gaps"))
        have = list(self.ranker().keywords(jd).have) if jd else []
        company, role = without_region(values.get("Company") or ""), values.get("Role") or ""
        llm = self.resume.llm(self.deps.config()) if self.resume is not None else None
        qs = (interview.questions(llm, company=company, role=role, jd=jd,
                                  resume_text=self._master_text(), gaps=gaps,
                                  key=job_id.replace("-", ""),
                                  facts=apply_facts.facts(self.s, self.deps.config()))
              if llm is not None else interview.Questions(note="no AI in this environment"))
        config = self.deps.config()
        ask = [line.strip(" -") for line in str(config.get("interview.ask") or "").splitlines()
               if line.strip(" -")] or list(self.s.interview.get("ask") or [])
        text = interview.prep_text(
            company=company, role=role, where=header.place(values) or "unknown place",
            url=values.get("URL") or "", have=have, gaps=gaps,
            details=[d for d in specific_details(body) if not names_gap(d, gaps)],
            qs=qs, ask=ask, tips=self.tips(tips_mod.INTERVIEW, values.get("Country")))
        return [Reply(text[:MAX_TEXT])]

    def thanks_command(self, args: str) -> list[Reply]:
        """/thanks <job> [first name]: the thank-you mail after an interview (feature 15)."""
        from jobengine.apply.pack import names_gap
        from jobengine.resume.builder import specific_details

        ref, _, who = args.strip().partition(" ")
        found = self._interview_job(ref, "Send /thanks <job URL or page id> <interviewer first "
                                         "name>.")
        if isinstance(found, Reply):
            return [found]
        job_id, values = found
        body = self.repo.read_body(job_id) if self.repo else []
        gaps = split_gaps(values.get("Gaps"))
        detail = next((d.strip() for d in specific_details(body)
                       if d.strip() and not names_gap(d, gaps)), None)
        config = self.deps.config()
        template = str(config.get("interview.thanks") or self.s.interview.get("thanks") or "")
        if not template:
            return [Reply("No thank-you template: add interview.thanks in config/base.yaml or "
                          "Notion Config.")]
        first = who.split()[0] if who.split() else None
        name = " ".join(str(config.get("profile.name") or "").split())
        return [Reply(interview.thanks_text(
            template, first=first, company=without_region(values.get("Company") or ""),
            role=values.get("Role") or "", detail=detail, name=name))]

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
            replies = self._then_drafts(Reply(result.message), job_id, result.contacts)
            self._add_connections(replies[0], job_id, values, result.contacts)
            return replies
        reply = _contacts_reply(result)
        self._add_connections(reply, job_id, values, [])
        return [reply]

    # ------------------------------------------------------------ referrals (feature 1)

    def _connections_at(self, values: dict[str, Any]) -> list[connections.Match]:
        if self.connections is None or not values.get("Company"):
            return []
        try:
            rows = self.connections.rows_like(values["Company"])
        except Exception as exc:  # the contacts answer still comes without them
            log.warning("LinkedIn connections not read: %s", exc)
            return []
        return connections.at_company(rows, values["Company"], values.get("Country") or "")

    def _add_connections(self, reply: Reply, job_id: str, values: dict[str, Any],
                         found: list[Any]) -> None:
        """Your LinkedIn connections at the company under the contacts answer, with a
        "Referral asks" button. People the finder found who are also your connections give
        the connection a country and work email (layer 1, free)."""
        matches = self._connections_at(values)
        if not matches:
            return
        if found and self.connections is not None:
            try:
                connections.learn_from_contacts(self.connections, matches, found)
                matches = self._connections_at(values)
            except Exception as exc:
                log.warning("LinkedIn connections not updated from contacts: %s", exc)
        company = without_region(values.get("Company") or "")
        reply.text += "\n\n" + connections.block(matches, company,
                                                  values.get("Country") or "", self.today())
        reply.buttons = [*reply.buttons, ("Referral asks", f"rf:{short_id(job_id)}")]
        if self.contacts is not None and any(m.row.person.url and not m.country
                                             for m in matches):
            reply.buttons = [*reply.buttons, ("Location and email",
                                              f"rl:{short_id(job_id)}")]

    def referral_tap(self, job_id: str) -> list[Reply]:
        """A LinkedIn message per connection at the company (the peer note: they are already
        connected, so it is a message, not an invitation), and "Mail them" when some have an
        email."""
        from jobengine.resume.builder import notion_id

        if self.repo is None:
            return [Reply(NO_TARGET)]
        job_id = notion_id(job_id)
        values = self.repo.get_values(job_id) or {}
        matches = self._connections_at(values)
        if not matches:
            return [Reply(f"No LinkedIn connection at {values.get('Company') or 'this company'} "
                          "in the 🤝 LinkedIn Connections table.")]
        today = self.today()
        reply = Reply(self.referral_messages(values, matches)[:MAX_TEXT])
        can_mail = connections.mailable(matches, today)
        if can_mail and self.mail is not None:
            reply.buttons = [(f"Mail them ({len(can_mail)})", f"rm:{short_id(job_id)}")]
            reply.text += (f"\n\n{len(can_mail)} of them have an email: Mail them adds them as "
                           "contacts of this job, then Write Gmail drafts uses the referral "
                           "template word for word.")
        return [reply]

    def referral_messages(self, values: dict[str, Any],
                          matches: list[connections.Match]) -> str:
        """A LinkedIn message per connection (the peer note), to send yourself."""
        from jobengine.contacts import linkedin_notes

        config = self.deps.config()
        base = self.s.apply_pack.get("linkedin_notes") or {}
        template = (config.get("linkedin.note_peer") or base.get("peer")
                    or linkedin_notes.DEFAULTS["peer"])
        me = config.get("linkedin.signature") or self.s.apply_pack.get("linkedin_signature")
        company = without_region(values.get("Company") or "")
        today = self.today()
        blocks = [f"Referral asks for {values.get('Role') or 'the role'} at {company}: send "
                  "each one yourself as a LinkedIn message (the bot never opens LinkedIn)."]
        for m in matches[:connections.MAX_SHOWN]:
            p = m.row.person
            asked = m.asked_on(today)
            note = linkedin_notes.note(template, name=p.name, role=values.get("Role") or "",
                                       company=company, link=values.get("URL") or "",
                                       me=str(me or ""))
            head = f"{p.name} ({p.position or m.kind}, {m.country or 'location unknown'})"
            if asked:
                head += f": asked on {asked:%d %b}, wait before asking again"
            where = p.url or f'search "{p.name} {company}" on LinkedIn'
            blocks.append(f"{head}\n{where}\n{note}")
        return "\n\n".join(blocks)

    def connection_lookup_tap(self, job_id: str) -> list[Reply]:
        """Prospeo by the LinkedIn profile link for at most MAX_ON_DEMAND connections at the
        company without a country (1 credit each when found)."""
        from jobengine.contacts import connection_lookup as lookup
        from jobengine.resume.builder import notion_id

        c = self.contacts
        if self.repo is None or c is None or self.connections is None:
            return [Reply("Contacts are not available in this bot.")]
        if not paid_api_allowed("prospeo", self.s) or not self.s.prospeo_api_key:
            return [Reply("Prospeo is not asked here: " + contact_lookups(self.s))]
        job_id = notion_id(job_id)
        values = self.repo.get_values(job_id) or {}
        picks = [m.row for m in self._connections_at(values)
                 if m.row.person.url and not m.country][:lookup.MAX_ON_DEMAND]
        if not picks:
            return [Reply("Every connection there already has a country.")]
        request = c.request("prospeo")
        key = str(self.s.prospeo_api_key)
        out = lookup.run(self.connections, picks,
                         lambda row: lookup.prospeo_lookup(row, request, key),
                         pause=lambda: c.sleep(lookup.PROSPEO_PAUSE))
        company = without_region(values.get("Company") or "")
        text = lookup.summary("Prospeo", out, f"at {company}")
        matches = self._connections_at(values)
        text += "\n\n" + connections.block(matches, company, values.get("Country") or "",
                                            self.today())
        return [Reply(text[:MAX_TEXT], [("Referral asks", f"rf:{short_id(job_id)}")])]

    def leftover_tick(self) -> list[Reply]:
        """Your idea of 23 Oct: on the last day before Prospeo or Hunter renews, the credits
        left are spent on your connections (Target Companies first). Asked once a day."""
        from jobengine.contacts import balance
        from jobengine.contacts import connection_lookup as lookup

        c = self.contacts
        if c is None or self.connections is None or self.state is None:
            return []
        live = [name for name, key in (("prospeo", self.s.prospeo_api_key),
                                       ("hunter", self.s.hunter_api_key))
                if key and paid_api_allowed(name, self.s)]
        today = self.today()
        if not live or lookup.checked_today(self.state, today):
            return []
        replies: list[Reply] = []
        rows: list[Any] | None = None
        for name in live:
            request = c.request(name)
            try:
                if name == "prospeo":
                    left, days = balance.prospeo_account(self.s, request)
                    renews = lookup.renewal_from_days(days, today)
                else:
                    left, reset = balance.hunter_account(self.s, request)
                    renews = (lookup.renewal_from_date(reset) or lookup.renewal_from_day(
                        self.s.contacts.get("hunter_renewal_day"), today))
            except (http.HttpError, ValueError, TypeError) as exc:
                log.warning("%s balance not read for the leftover run: %s", name, exc)
                continue
            if not lookup.leftover_due(self.state, name, left, renews, today):
                continue
            assert renews is not None and left is not None
            if rows is None:
                rows = self.connections.rows()
            targets = list(c.reference().companies)
            limit = min(left, lookup.MAX_LEFTOVER)
            if name == "prospeo":
                key = str(self.s.prospeo_api_key)
                out = lookup.run(self.connections, lookup.prospeo_picks(rows, targets, limit),
                                 lambda row, r=request, k=key: lookup.prospeo_lookup(row, r, k),
                                 pause=lambda: c.sleep(lookup.PROSPEO_PAUSE))
            else:
                key = str(self.s.hunter_api_key)
                out = lookup.run(self.connections, lookup.hunter_picks(rows, targets, limit),
                                 lambda pick, r=request, k=key: lookup.hunter_lookup(
                                     pick[0], pick[1], r, k))
            lookup.mark_done(self.state, name, renews)
            why = f"{left} credits left, new credits on {renews:%d %b}"
            replies.append(Reply(lookup.summary(name.title(), out, why)))
        return replies

    def referral_mail_tap(self, job_id: str) -> list[Reply]:
        """Adds the connections with an email as Peer contacts of the job (Source Other, a
        Notes line saying LinkedIn connection) and marks them Asked in the table."""
        from jobengine.resume.builder import notion_id

        if self.repo is None or self.contacts is None or self.contacts.contacts is None:
            return [Reply("Contacts are not available in this bot.")]
        job_id = notion_id(job_id)
        values = self.repo.get_values(job_id) or {}
        today = self.today()
        matches = connections.mailable(self._connections_at(values), today)
        if not matches:
            return [Reply("No connection to mail: none has an email, or all were asked in "
                          f"the last {connections.ASKED_DAYS} days.")]
        chosen = self.add_referral_contacts(job_id, values, matches)
        names = ", ".join(m.row.person.name for m in matches)
        return self._then_drafts(Reply(f"Added as contacts of this job: {names}. They are "
                                       "marked Asked in 🤝 LinkedIn Connections."),
                                 job_id, chosen)

    def add_referral_contacts(self, job_id: str, values: dict[str, Any],
                              matches: list[connections.Match]) -> list[Any]:
        """Connections with an email become Peer contacts of the job (Source Other, a Notes
        line saying LinkedIn connection) and are marked Asked in the table."""
        from jobengine.contacts.models import PEER, Chosen

        assert self.contacts is not None and self.contacts.contacts is not None
        today = self.today()
        by_email = {(v.get("Email") or "").strip().lower(): pid
                    for pid, v in self.contacts.contacts.all_rows() if v.get("Email")}
        chosen = []
        for m in matches:
            country = m.country if m.country in ("Poland", "Netherlands", "Ireland") else "Other"
            chosen.append(Chosen(
                name=m.row.person.name, title=m.row.person.position, email=m.email, type=PEER,
                source="Other", country=country,
                notes=f"LinkedIn connection ({m.kind}); referral ask",
                page_id=by_email.get(m.email.lower())))
        contact_finder.write_contacts(self.contacts, job_id, values, chosen, today)
        if self.connections is not None:
            connections.mark_asked(self.connections, matches, today)
        return chosen

    def _then_drafts(self, first: Reply, job_id: str, contacts: list[Any]) -> list[Reply]:
        """The contacts summary with a "Write Gmail drafts" button: drafts are only written
        when you tap it."""
        contact_finder.on_contacts_ready(job_id, contacts)
        if _contact_ids(contacts):  # notes to copy into LinkedIn yourself (feature 12)
            first.buttons = [*first.buttons, ("LinkedIn notes", f"ln:{short_id(job_id)}")]
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

    def linkedin_notes(self, job_id: str) -> list[Reply]:
        """The "LinkedIn notes" button: a connection note per contact (feature 12)."""
        from jobengine.contacts import linkedin_notes
        from jobengine.resume.builder import notion_id

        if self.repo is None or self.contacts is None or self.contacts.contacts is None:
            return [Reply("Contacts are not available in this bot.")]
        job_id = notion_id(job_id)
        values = self.repo.get_values(job_id) or {}
        wanted = {short_id(cid) for cid in values.get("Contacts") or []}
        rows = [v for pid, v in self.contacts.contacts.all_rows() if short_id(pid) in wanted]
        config = self.deps.config()
        base = self.s.apply_pack.get("linkedin_notes") or {}
        templates = {kind: config.get(f"linkedin.note_{kind}") or base.get(kind)
                     for kind in ("peer", "recruiter", "hiring")}
        me = config.get("linkedin.signature") or self.s.apply_pack.get("linkedin_signature")
        text = linkedin_notes.notes(
            rows, role=values.get("Role") or "",
            company=without_region(values.get("Company") or ""),
            link=values.get("URL") or "", me=str(me or ""),
            templates={k: v for k, v in templates.items() if v})
        return [Reply(text[:MAX_TEXT])]

    # ------------------------------------------------------------ /fetchcontacts

    def applied_on(self, day: date) -> list[tuple[str, dict[str, Any]]]:
        """Jobs you marked I applied on this day, oldest row first."""
        from jobengine.notion_repo import select_filter

        assert self.repo is not None
        return [(pid, values) for pid, values in
                self.repo.query_rows(select_filter("Status", "Applied"))
                if values.get("Applied date") == day]

    def next_command(self) -> list[Reply]:
        """/next: today's steps in order, what is done and what to do now (9 Oct)."""
        from jobengine import next_steps
        from jobengine.sweep.runner import LAST_SUMMARY

        if self.repo is None:
            return [Reply(NO_TARGET)]
        today = self.today()
        last = self.state.get(LAST_SUMMARY) or {}
        waiting_rows = waiting_for_jd(self.repo, self.state)
        waiting_ids = {r.page_id for r in waiting_rows}
        unscreened = sum(1 for pid, _ in self.repo.query_rows(
            select_filter("Screen verdict", "Unscreened")) if pid not in waiting_ids)
        applied = self.applied_on(today) if self.contacts is not None else []
        state = next_steps.DayState(
            fetched_today=last.get("at") == today.isoformat(),
            unscreened=unscreened, waiting_jd=len(waiting_rows),
            pending=len(pending_rows(self.repo)), applied_today=len(applied),
            contacts_done=bool(applied) and all(v.get("Contacts") for _, v in applied),
            also=tuple(self._also_due(today)))
        return [Reply(next_steps.checklist(state))]

    def _also_due(self, today: date) -> list[str]:
        """/next: the weekly and monthly steps that are due (27 Oct, Command guide)."""
        from jobengine import next_steps
        from jobengine.track import health_report

        out = next_steps.periodic_due(self.state, today)
        if self.strategy is not None and strategy_runner.open_review(self.strategy):
            out.append("/update: strategy tips wait for your Adopt or Reject")
        else:
            gate = health_report.gate_days(self.deps.config(), today)
            if gate is not None and gate <= health_report.GATE_SOON_DAYS:
                out.append("/update: the monthly strategy review, "
                           + (f"/fetch closes in {gate} day(s)" if gate > 0
                              else "/fetch is blocked until it is done"))
        when, _count = connections.last_import(self.state)
        if when is not None and (today - when).days >= connections.REFRESH_DAYS:
            out.append("LinkedIn export: send the new Connections zip to the bot (/connections)")
        return out

    def note_command(self, command: str | None) -> None:
        """Remember when you last used /stats, /gaps or /keywords (for /next)."""
        from jobengine import next_steps

        if command:
            next_steps.mark_used(self.state, command, self.today())

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
            if _agency(values):
                what = "agency post, no cold mails"
            lines.append(f"- {values.get('Company')}, {values.get('Role')}: {what}")
        need = len(jobs) - len(saved)
        if need:
            lines.append(f"{need} job(s) need a lookup: the Contacts cache and the job posting "
                         "first, then the paid providers (/credits shows what is left).")
            if not paid_api_allowed("apollo", self.s):
                lines.append(f"Contact lookups: {contact_lookups(self.s)}.")
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
        agency: list[str] = []
        c = self.contacts
        for index, (job_id, values) in enumerate(jobs, 1):
            job = f"{values.get('Company')}, {values.get('Role')}"
            emit(Reply(f"{index}/{len(jobs)} {job}..."))
            if _agency(values):
                emit(Reply(f"{job}: a recruitment agency's post, no cold mails (apply through "
                           "the agency; /contacts <job> if you still want them)."))
                agency.append(job)
                continue
            if not values.get("Contacts"):
                planner.force(c.state, c.config(), job_id, c.today())  # still counted
                found = contact_finder.find_contacts(c, job_id)
                if found.status == "waiting_domain":
                    emit(_contacts_reply(found))  # answer it; then tap Write Gmail drafts
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
            lines.append("Waiting for the email domain (answer the question, then tap Write "
                         "Gmail drafts): " + "; ".join(waiting))
        if failed:
            lines.append("Drafts not written (see the message above): " + "; ".join(failed))
        if agency:
            lines.append("Agency posts, no cold mails: " + "; ".join(agency))
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
        from jobengine.contacts.credits import (
            PROVIDERS,
            SECOND,
            account_label,
            monthly_reset,
            parse_counter,
        )

        config = (self.contacts.config if self.contacts else self.deps.config)()
        keys = {"apollo": self.s.apollo_api_key, "hunter": self.s.hunter_api_key,
                "snov": self.s.snov_client_id and self.s.snov_client_secret,
                "prospeo": self.s.prospeo_api_key,
                "tomba": self.s.tomba_api_key and self.s.tomba_api_secret,
                "apollo_2": self.s.apollo_api_key_2, "hunter_2": self.s.hunter_api_key_2,
                "snov_2": self.s.snov_client_id_2 and self.s.snov_client_secret_2,
                "prospeo_2": self.s.prospeo_api_key_2,
                "tomba_2": self.s.tomba_api_key_2 and self.s.tomba_api_secret_2}
        lines = ["Provider credits (reset on the 1st):"]
        for provider in (*PROVIDERS, *SECOND):
            key = f"credits.{provider}"
            if provider in SECOND and not keys[provider] and not config.get(key):
                continue  # no second account for this provider
            counter = parse_counter(config.get(key), config.updated(key))
            updated = config.updated(key)
            if counter:
                reset = monthly_reset(counter, self.today())
                value = reset.text() + (" (reset for this month)" if reset.used != counter.used
                                        else "")
            elif provider in SECOND:
                value = f"no Config {key} row (counted per run only)"
            else:
                value = config.get(key) or "not set"
            when = f", updated {updated.isoformat()}" if updated else ""
            key_state = "key set" if keys[provider] else "no key"
            lines.append(f"{account_label(provider)}: {value}{when}, {key_state}")
        lines.append(f"Contact lookups: {contact_lookups(self.s)}.")
        if paid_api_allowed("hunter", self.s):
            # 22 Oct: the counters above are the bot's own count (only simulated on the PC);
            # the providers' own numbers are the truth.
            from jobengine.contacts import balance
            from jobengine.contacts.providers.base import real_request

            lines += ["", "Real credits left (asked from each provider now, free):",
                      *balance.lines(self.s, lambda name: real_request(self.s))]
        return [Reply("\n".join(lines))]

    def domain_answer(self, replied_to: str, text: str) -> list[Reply] | None:
        """An answer to "What is the email domain for ...? Ref JOB-xxxxxxxx": a Telegram
        reply to it, a tapped domain button, or (domain_text) the domain sent on its own."""
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

    def domain_text(self, text: str) -> list[Reply] | None:
        """A plain message that is only a domain, while a domain question is open (Phase 6:
        the answer did not count unless sent as a Telegram reply)."""
        word = text.strip()
        if self.contacts is None or not contact_finder.looks_like_domain(word):
            return None
        asked = contact_finder.open_question(self.contacts.state)
        if asked is None:
            return None
        return self.domain_answer(f"Ref {asked['ref']}", word)

    def domain_tap(self, arg: str) -> list[Reply]:
        """A domain button under the question: dm:JOB-xxxxxxxx:example.com."""
        ref, _, domain = arg.partition(":")
        replies = self.domain_answer(f"Ref {ref}", domain)
        return replies or [Reply("That domain question is no longer open.")]

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
        message = result.message
        if result.drafted:
            values = (self.repo.get_values(job_id) if self.repo else None) or {}
            message += tips_mod.lines("Outreach tips",
                                      self.tips(tips_mod.OUTREACH, values.get("Country")))
        return [Reply(message[:MAX_TEXT])]

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

    def fetchreport_command(self, args: str = "") -> list[Reply]:
        """/fetchreport [word]: where the postings of the last /fetch went, per source, and
        the dropped jobs by reason (FETCH 10)."""
        from jobengine.sweep.models import LossReport
        from jobengine.sweep.runner import LAST_REPORT, SOURCE_LABELS

        value = self.state.get(LAST_REPORT)
        if not value:
            return [Reply("No /fetch has run yet (or it ran before this report existed). "
                          "Send /fetch first.")]
        report = LossReport.from_state(value)
        text = report.report_text(dict(SOURCE_LABELS), str(value.get("at") or "?"), args)
        # Telegram's / menu cannot suggest the words after a command: one button per big
        # reason opens it instead (1 Oct test R3).
        top = [] if args.strip() else report.reasons()[:REPORT_BUTTONS]
        buttons = [(f"{reason} ({n})", f"fr:{reason}"[:60]) for reason, n in top]
        return [Reply(text[:MAX_TEXT], buttons)]

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
        per_job: list[list[str]] = []
        for _pid, values in self.repo.query_rows():
            gaps = [g for g in split_gaps(values.get("Gaps")) if g.casefold() not in NO_GAP]
            if not gaps:
                continue
            rows += 1
            per_job.append(gaps)
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
        lines += learning.plan(per_job, self.s.learning.get("certificates") or {})
        lines.append("")
        lines.append("Learned one? Add it to Skills Inventory as Hands-on or Production: "
                     "screening and resumes then count it as yours.")
        replies = [Reply("\n".join(lines)[:MAX_TEXT])]
        if self.add_skill is not None:
            replies += self._skill_buttons([names[key] for key, _ in ranked])
        return replies

    def connections_command(self) -> list[Reply]:
        """/connections: when the LinkedIn connections were last updated and how to update."""
        return [Reply(connections.status(self.state, self.today(),
                                         self.connections is not None))]

    def connections_file(self, name: str, data: bytes) -> list[Reply]:
        """Your LinkedIn export (zip or Connections.csv), sent to the bot as a file."""
        if self.connections is None:
            return self.connections_command()

        def say(text: str) -> None:
            if self.notify is not None:
                self.notify(Reply(text))

        return [Reply(connections.run_import(self.connections, self.state, data, name,
                                             self.today(), say))]

    def connections_reminder(self) -> list[Reply]:
        """The monthly reminder to send a new export (once a week while it is due)."""
        if self.connections is None:
            return []
        text = connections.reminder(self.state, self.today())
        return [Reply(text)] if text else []

    def keywords_command(self) -> list[Reply]:
        """/keywords: the tools your best jobs name most, against your LinkedIn profile text
        (feature 8)."""
        if self.repo is None:
            return [Reply(NO_TARGET)]
        best = [(pid, v) for pid, v in self.repo.query_rows()
                if v.get("Status") in keywords_report.BEST]
        best.sort(key=lambda row: str(row[1].get("First seen") or ""), reverse=True)
        jobs = []
        for pid, _values in best[:keywords_report.MAX_JOBS]:
            jd, _kind = description(self.repo.read_body(pid))
            if jd:
                found = self.ranker().keywords(jd)
                jobs.append((list(found.have), list(found.missing)))
        config = self.deps.config()
        profile = " ".join(str(config.get(k) or "") for k in keywords_report.PROFILE_KEYS)
        same = keywords_report.aliases(self.reference().term_map)
        return [Reply(keywords_report.report(jobs, profile, same)[:MAX_TEXT])]

    def _skill_buttons(self, gaps: list[str]) -> list[Reply]:
        """One small message per short gap name with Hands-on and Production buttons (your
        idea of 5 Oct): only you decide which skills you have and at which level."""
        ref = self.reference()
        out: list[Reply] = []
        for name in gaps:
            data = f"gs:p:{name}"
            if (len(name.split()) > MAX_SKILL_WORDS or len(data.encode()) > 64
                    or ref.lookup(name) is not None):
                continue  # a sentence, not a skill name; or already yours
            out.append(Reply(f"{name}: do you have it?",
                             [("Hands-on", f"gs:h:{name}"), ("Production", data)]))
            if len(out) >= MAX_SKILL_BUTTONS:
                break
        if out:
            out.insert(0, Reply("Tap Hands-on (projects or labs) or Production (used at work) for "
                                "a skill you already have. Skip the ones you do not."))
        return out

    def skill_tap(self, arg: str) -> list[Reply]:
        code, _, name = arg.partition(":")
        level = {"h": "Hands-on", "p": "Production"}.get(code)
        if self.add_skill is None or not level or not name:
            return [Reply("That button is no longer valid. Send /gaps.")]
        if self.reference().lookup(name) is not None:
            return [Reply(f"{name} is already in your skills.")]
        try:
            self.add_skill(name, level, f"Added from /gaps on {self.today().isoformat()}.")
        except http.HttpError as exc:
            return [Reply(f"Could not add {name} to Skills Inventory: {exc}")]
        self._reference = None  # screening and resumes see it at once
        return [Reply(f"Added {name} to Skills Inventory as {level}. Screening, /pending cards "
                      "and resumes count it from now on. Category and Where used can be filled "
                      "in Notion.")]

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
            pings = ping.check_pings(self.track, now)
        except Exception as exc:
            log.warning("reply check failed: %s", exc)
            return self._token_alert(str(exc), now.date())
        texts = [p.text for p in pings]
        day_ledger.count("replies", len(texts))
        if any(ping.INTERVIEW_HINT in text for text in texts):
            extra = tips_mod.lines("Interview tips", self.tips(tips_mod.INTERVIEW, None))
            texts = [text + extra if ping.INTERVIEW_HINT in text else text for text in texts]
        out: list[Reply] = []
        for p, text in zip(pings, texts, strict=True):
            out.append(Reply(text[:MAX_TEXT]))
            if p.interview and p.job_id:
                out += self.auto_prep(p.job_id, now.date())
        return out

    def auto_prep(self, job_id: str, today: date) -> list[Reply]:
        """Autopilot D: a reply about an interview or a call comes with the /prep pack for
        that job, once a day per job (one AI call; a failure only loses the pack)."""
        done = dict(((self.state.get(AUTO_PREP_KEY) or {}).get("jobs")) or {})
        if done.get(job_id) == today.isoformat():
            return []
        try:
            replies = self.prep_command(job_id)
        except Exception as exc:  # the ping still comes
            log.warning("auto /prep failed for %s: %s", job_id, exc)
            return []
        done[job_id] = today.isoformat()
        recent = sorted(done.items(), key=lambda kv: kv[1])[-50:]
        self.state.set(AUTO_PREP_KEY, {"jobs": dict(recent)})
        return [Reply("\U0001F3AF Prepare for it now:"), *replies]

    # ------------------------------------------------------------ daily report (H1, H3)

    def daily_report_text(self) -> str:
        """/report and the evening report: what the bot did today (track/daily_report.py)."""
        from jobengine import gmail_renew
        from jobengine.contacts.credits import CreditBook
        from jobengine.screen import schedule
        from jobengine.track import costs, daily_report

        config = self.deps.config()
        today = daily_report.local(self.tracking_now(), config).date()
        waiting: list[str] = []
        if self.repo is not None:
            try:
                waiting = [f"{len(pending_rows(self.repo))} job(s) in /pending",
                           f"{len(waiting_for_jd(self.repo, self.state))} need a description "
                           "(/jd)"]
            except Exception as exc:  # the report still comes without these two counts
                log.warning("daily report: could not count waiting jobs: %s", exc)
        entry = day_ledger.day(self.state, today)
        history = [day_ledger.day(self.state, today - timedelta(days=k))
                   for k in range(1, daily_report.HISTORY_DAYS + 1)]
        book = CreditBook(config, today)
        warned = daily_report.warnings(
            entry, history, tokens=gmail_renew.token_left(self.s, config, today),
            used_up=book.used_up(), missed=schedule.missed(self))
        return daily_report.text(
            entry, today, month_cost=costs.month_line(self.state, today), credits=book.line(),
            tokens=gmail_renew.days_left(self.s, config, today), waiting=waiting,
            warned=warned)

    def report_command(self) -> list[Reply]:
        return [Reply(self.daily_report_text()[:MAX_TEXT])]

    def table_query(self, source: str) -> Any:
        """Read one row of a Notion table (the morning check); raises when it cannot."""
        if not self.s.notion_token:
            raise RuntimeError("NOTION_TOKEN is not set")
        from jobengine.notion_repo import NotionClient

        client = NotionClient(self.s.notion_token, self.s)
        return client.request("POST", f"/data_sources/{source}/query", {"page_size": 1})

    def health_report_text(self) -> str:
        """The morning check (Autopilot C, track/health_report.py): what would stop the run
        first, with the fix."""
        from jobengine import gmail_renew
        from jobengine.contacts import connections as connections_mod
        from jobengine.contacts.credits import CreditBook
        from jobengine.screen import schedule
        from jobengine.strategy import auto_update
        from jobengine.track import costs, daily_report, health_report

        config = self.deps.config()
        today = daily_report.local(self.tracking_now(), config).date()
        blockers, soon, fine = [], [], []
        found = health_report.tables(self.s.notion_read, self.s.notion_write)
        problems = health_report.table_problems(found, self.table_query)
        blockers += problems
        if not problems:
            fine.append(f"Notion: {len(found)} tables reachable")
        if self.track is not None:
            for name in ("alerts", "sender"):
                reason = self.track.token_check(name)
                if reason is not None:
                    blockers.append(f"Gmail {name} token: {reason}. Fix: /gmailtoken")
            if self.s.anthropic_api_key:
                try:
                    problem = self.track.key_check()
                except Exception as exc:  # report, never raise
                    problem = f"not checked ({type(exc).__name__})"
                if problem is not None:
                    blockers.append(f"Anthropic key: {problem}")
            else:
                blockers.append("Anthropic key: missing in .env")
        for account, left in gmail_renew.token_left(self.s, config, today):
            if left <= health_report.TOKEN_SOON_DAYS:
                soon.append(f"Gmail token {account}: {max(left, 0)} day(s) left. Fix: "
                            f"/gmailtoken {account}")
        gate = health_report.gate_days(config, today)
        if gate is None or gate < 0:
            blockers.append("The /fetch gate is closed: run /update (the monthly strategy "
                            "check).")
        elif gate <= health_report.GATE_SOON_DAYS:
            soon.append(f"/update is due in {gate} day(s), then /fetch stops until you run "
                        "it." + (" The bot runs the research by itself (once, from 07:00); "
                                 "you adopt or reject the tips." if auto_update.is_on(config)
                                 else ""))
        else:
            fine.append(f"/fetch gate open for {gate} more days")
        book = CreditBook(config, today)
        if book.used_up():
            soon.append(f"No contact credit left this month: {', '.join(book.used_up())}")
        fine.append(book.line())
        when, _count = connections_mod.last_import(self.state)
        if when is not None and (today - when).days >= connections_mod.REFRESH_DAYS:
            soon.append(f"LinkedIn connections export is {(today - when).days} days old: "
                        "send a new one (/connections).")
        plan = schedule.plan(self)
        if not plan.on:
            soon.append("The autopilot schedule is off (Notion Config schedule.autopilot).")
        fine.append(costs.month_line(self.state, today))
        from jobengine.sweep.runner import LAST_SUMMARY

        last = (self.state.get(LAST_SUMMARY) or {}).get("at")
        fine.append(f"Last /fetch: {last or 'never'}")
        starts = plan.times_text() if plan.on else ""
        return health_report.text(today, blockers, soon, fine, starts)

    def health_report_tick(self) -> list[Reply]:
        """Every few minutes (and POST /tasks/health): the morning check, once a day."""
        from jobengine.track import daily_report, health_report

        config = self.deps.config()
        now = self.tracking_now()
        if not health_report.due(config, self.state, now):
            return []
        health_report.mark_sent(self.state, daily_report.local(now, config).date())
        return [Reply(self.health_report_text()[:MAX_TEXT])]

    def daily_report_tick(self) -> list[Reply]:
        """Every few minutes (and POST /tasks/report): the evening report, once a day, on
        Telegram and by email."""
        from jobengine.track import daily_report

        config = self.deps.config()
        now = self.tracking_now()
        if not daily_report.due(config, self.state, now):
            return []
        daily_report.mark_sent(self.state, daily_report.local(now, config).date())
        text = self.daily_report_text()
        why = self.owner_mail(f"Job Engine daily report {now:%d %b}", text)
        if why:
            text += f"\n(The email copy was not sent: {why})"
        self.heartbeat(True)  # no report for about 26 hours: healthchecks.io emails you
        return [Reply(text[:MAX_TEXT])]

    def alert_failure(self, what: str, error: object) -> None:
        """A scheduled run failed: mail you at once (at most once an hour per run, H3)."""
        try:
            if alerts.mail_failure(self.state, self.owner_mail, what, str(error),
                                   self.tracking_now()):
                self.heartbeat(False)
        except Exception:  # an alert must never stop the bot
            log.exception("could not mail the failure of %s", what)

    def _token_alert(self, error: str, today: date) -> list[Reply]:
        """An expired Gmail token (invalid_grant) is told in Telegram once a day; before
        7 Oct it only filled the log every 5 minutes and /health was the only way to see it."""
        if not any(word in error for word in TOKEN_ERRORS):
            return []
        if (self.state.get(TOKEN_ALERT_KEY) or {}).get("day") == today.isoformat():
            return []
        self.state.set(TOKEN_ALERT_KEY, {"day": today.isoformat()})
        return [Reply(TOKEN_ALERT)]

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
        text = track_commands.health_text(self.track)
        try:
            self.permits()
            text += f"\nIrish permits list: {self.permits_note}"
        except Exception as exc:  # /health never fails over the permits list
            text += f"\nIrish permits list: not checked ({type(exc).__name__})"
        return [Reply(text)]

    def agencies_command(self, args: str) -> list[Reply]:
        """/agencies: recruitment agencies by country; /agencies <name> the intro mail;
        /agencies sent <name> records the contact (feature 7)."""
        from jobengine.contacts import agencies as ag

        found = ag.load(self.s)
        config = self.deps.config()
        text = args.strip()
        sent = re.match(r"(?:sent|done)\s+(.+)", text, re.I)
        query = sent.group(1) if sent else text
        if not query:
            return [Reply(ag.listing(found, self.state, config)[:MAX_TEXT])]
        matches = ag.find(found, query)
        if len(matches) != 1:
            names = ", ".join(a.name for a in (matches or found))
            return [Reply(f"Which agency? {names}")]
        agency = matches[0]
        if sent:
            ag.mark(self.state, agency, self.today())
            return [Reply(f"Recorded: {agency.name} contacted on {self.today().isoformat()}. "
                          f"The digest reminds you after {ag.FOLLOW_UP_DAYS} days.")]
        return [Reply(ag.intro(self.s, config, agency)[:MAX_TEXT])]

    # ------------------------------------------------------------ apply nudge (H7)

    def _nudge_here(self) -> datetime:
        from jobengine.track import daily_report

        return daily_report.local(self.tracking_now(), self.deps.config())

    def _ready(self) -> list[str]:
        """Jobs whose resume is built and that wait for you to apply, oldest first."""
        from jobengine.track import nudge

        if self.repo is None:
            return []
        return nudge.ready_ids(self.repo.query_rows(select_filter("Status", "Resume built")))

    def nudge_on(self) -> bool:
        from jobengine.track import nudge

        return nudge.active(self.state, self._nudge_here())

    def nudge_tick(self) -> list[Reply]:
        """From 22:00 India time: the first message, then a reminder every 30 minutes."""
        from jobengine.track import nudge

        config = self.deps.config()
        here = self._nudge_here()
        ready = self._ready()
        if not nudge.due(config, self.state, here, len(ready)):
            return []
        evening = nudge.evening_of(here)
        value = nudge.get(self.state, evening)
        minutes = nudge.minutes_per_job(config)
        if not value.get("sent"):
            text = nudge.first_text(len(ready), minutes, evening.weekday() >= 5,
                                    nudge.streak(self.state, evening))
            reply = Reply(text, [*nudge.energy_buttons(), *nudge.later_buttons()], column=True)
        else:
            target = nudge.goal(value, len(ready))
            text = nudge.reminder_text(len(ready), int(value.get("applied") or 0), target,
                                       minutes)
            reply = Reply(text, [("▶️ Next job", "nj:1"), *nudge.later_buttons()], column=True)
        value.update(sent=int(value.get("sent") or 0) + 1, last=here.isoformat())
        nudge.save(self.state, value)
        return [reply]

    def nudge_card(self) -> list[Reply]:
        """The next ready job, with I applied, Not applying and Done for today."""
        from jobengine.track import nudge

        here = self._nudge_here()
        evening = nudge.evening_of(here)
        value = nudge.get(self.state, evening)
        ready = self._ready()
        target = nudge.goal(value, len(ready))
        applied = int(value.get("applied") or 0)
        if not ready or (target is not None and applied >= target):
            return self.nudge_finish()
        job_id = ready[0]
        values = (self.repo.get_values(job_id) if self.repo else None) or {}
        ref = short_id(job_id)
        text = nudge.card_text(values, applied + 1, target, ref)
        return [Reply(text, [("✅ I applied", f"ia:{ref}"), ("Not applying", f"na:{ref}"),
                             ("Done for today", "dd:1")])]

    def nudge_applied(self) -> list[Reply]:
        """After I applied tonight: count it, then the next job or the well done."""
        from jobengine.track import nudge

        evening = nudge.evening_of(self._nudge_here())
        value = nudge.get(self.state, evening)
        value["applied"] = int(value.get("applied") or 0) + 1
        nudge.save(self.state, value)
        nudge.record_streak(self.state, evening)
        return self.nudge_card()

    def nudge_finish(self) -> list[Reply]:
        from jobengine.track import nudge

        evening = nudge.evening_of(self._nudge_here())
        value = nudge.get(self.state, evening)
        value["done"] = True
        value["sent"] = max(int(value.get("sent") or 0), 1)
        nudge.save(self.state, value)
        applied = int(value.get("applied") or 0)
        days = nudge.record_streak(self.state, evening) if applied else 0
        return [Reply(nudge.finish_text(applied, days))]

    def nudge_tap(self, action: str, arg: str) -> list[Reply]:
        from jobengine.track import nudge

        here = self._nudge_here()
        value = nudge.get(self.state, nudge.evening_of(here))
        if action == "dd":
            return self.nudge_finish()
        if action == "sz":
            value["snooze"] = (here + nudge.SNOOZE).isoformat()
            value["sent"] = max(int(value.get("sent") or 0), 1)
            nudge.save(self.state, value)
            return [Reply(f"😴 Snoozed: the next reminder comes after "
                          f"{here + nudge.SNOOZE:%H:%M}.")]
        if action == "en" and arg in (*nudge.GOALS, "high"):
            value.update(goal=arg, done=False, sent=max(int(value.get("sent") or 0), 1))
            nudge.save(self.state, value)
            target = nudge.goal(value, len(self._ready()))
            head = Reply(f"Tonight's goal: {target} job(s). One at a time; tap I applied when "
                         "it is sent.")
            return [head, *self.nudge_card()]
        return self.nudge_card()

    # ------------------------------------------------------------ spot check (H5)

    def spot_check(self) -> list[Reply]:
        """2 of the week's approved resumes, with their match and cover letter (H5)."""
        from jobengine.mail.drafter import resume_pdf
        from jobengine.track import spot_check

        if self.resume is None or self.resume.resume_log is None or self.repo is None:
            return [Reply("The spot check needs the Resume Log; it is not available here.")]
        today = self.tracking_now().date()
        picks = spot_check.pick(self.resume.resume_log.all_rows(), today)
        if not picks:
            return [Reply("🔍 Spot check: no resume was approved in the last 7 days.")]
        replies, cards = [], {}
        for log_id, row in picks:
            job_id = (row.get("Job") or [""])[0]
            values = (self.repo.get_values(job_id) if job_id else None) or {}
            job = f"{values.get('Company') or '?'}, {values.get('Role') or '?'}"
            body = self.repo.read_body(job_id) if job_id else []
            pdf = None
            if self.mail is not None:
                try:
                    pdf = resume_pdf(self.mail, dict(row))
                except Exception as exc:  # the card still comes without the PDF
                    log.info("spot check: no PDF for %s: %s", job, exc)
            jd, _kind = description(body)
            terms = list(self.ranker().keywords(jd).have) if jd else []
            match = cover.cover_line(terms, cover.pdf_text(pdf)) if pdf else None
            ref = spot_check.ref(log_id)
            cards[ref] = job
            text = spot_check.card(job, match, spot_check.letter(body), log_id)
            buttons = [("👍 Looks fine", f"sf:{ref}"), ("⚠️ Something is wrong", f"sw:{ref}")]
            name = str(row.get("Resume name") or "resume").removesuffix(".pdf") + ".pdf"
            replies.append(Reply(text[:1000] if pdf else text[:MAX_TEXT], buttons,
                                 document=(name, pdf) if pdf else None))
        spot_check.remember(self.state, cards)
        return replies

    def expired_check_tick(self) -> list[Reply]:
        """Every few minutes (and the digest task): the weekly link check, which marks open
        jobs whose posting closed Expired (Autopilot I1)."""
        from jobengine.track import daily_report, expired_check

        if self.repo is None:
            return []
        config = self.deps.config()
        now = daily_report.local(self.tracking_now(), config)
        if not expired_check.due(config, self.state, now):
            return []
        expired_check.mark_done(self.state, now.date())
        rows = [job_row(pid, values) for pid, values in self.repo.query_rows(
            select_filter("Status", *expired_check.OPEN_STATUSES))]
        result = expired_check.check(rows, self.get_page, now.date())
        expired_check.expire(result, self.repo.update)
        return [Reply(result.text()[:MAX_TEXT])]

    def spot_check_tick(self) -> list[Reply]:
        """Sunday 20:00 India time (with the digest): the spot check, once a week."""
        from jobengine.track import spot_check

        now = self.tracking_now()
        if not spot_check.due(self.state, now):
            return []
        spot_check.mark_sent(self.state, now.date())
        return self.spot_check()

    def spot_check_command(self, args: str) -> list[Reply]:
        """/spotcheck: a spot check now; /spotcheck notes: your notes for Claude Code."""
        from jobengine.track import spot_check

        if args.strip().lower() == "notes":
            return [Reply(spot_check.notes_text(self.state)[:MAX_TEXT])]
        return self.spot_check()

    def spot_tap(self, action: str, ref: str) -> list[Reply]:
        from jobengine.track import spot_check

        job = spot_check.job_of(self.state, ref)
        if action == "sf":
            return [Reply(f"👍 Thanks: {job} looks fine.")]
        return [Reply(spot_check.question(job, ref))]

    def spot_note(self, replied_to: str, text: str) -> list[Reply] | None:
        from jobengine.track import spot_check

        saved = spot_check.add_note(self.state, replied_to, text, self.tracking_now().date())
        return [Reply(saved)] if saved else None

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

    def auto_update_tick(self) -> list[Reply]:
        """Every few minutes (and the daily task): the monthly /update by itself, once, when
        the /fetch gate closes within 5 days (Autopilot I5)."""
        from jobengine.strategy import auto_update
        from jobengine.sweep.gate import last_update
        from jobengine.track import daily_report

        if self.strategy is None:
            return []
        config = self.deps.config()
        now = daily_report.local(self.tracking_now(), config)
        updated = last_update(config, self.strategy.state, self.strategy.s)
        if updated is None or not auto_update.due(
                config, self.strategy.state, updated, now,
                strategy_runner.open_review(self.strategy)):
            return []
        left = auto_update.days_left(updated, config.get_int("strategy_refresh_days"),
                                     now.date())
        result = strategy_runner.run_update(self.strategy)
        ok = strategy_runner.open_review(self.strategy)
        auto_update.mark(self.strategy.state, updated, now.date(), ok=ok)
        first = auto_update.intro(left or 0) if ok else auto_update.FAILED
        return [Reply(first), *(Reply(m[:MAX_TEXT]) for m in result.messages),
                *(Reply(c.text, c.buttons) for c in result.cards)]

    def strategy_tap(self, action: str, arg: str) -> list[Reply]:
        if self.strategy is None:
            return [Reply("/update is not available in this bot.")]
        if action == "sc":
            messages = strategy_runner.confirm_review(self.strategy, arg)
        elif action == "sx":
            messages = strategy_runner.reject_rest(self.strategy, arg)
        else:
            messages = strategy_runner.decide(self.strategy, arg, adopt=action == "sa")
        replies = [Reply(m[:MAX_TEXT]) for m in messages]
        run_id, left = strategy_runner.open_count(self.strategy)
        if left and replies:  # the last message gets the way to close the review at once
            replies[-1] = Reply(replies[-1].text, [(f"Reject the rest ({left})", f"sx:{run_id}")])
        return replies

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
        if summary.waiting_for_jd is None:  # /screen collect: the real count (8 Oct)
            summary.errors.append(self.waiting_line() or "No jobs wait for a description.")
        ready = len(pending_rows(self.repo))
        if not ready:
            return summary.text()
        if not summary.results:  # nothing screened now (stopped, or nothing to do)
            return f"{summary.text()}\n{ready} screened earlier still wait in /pending."
        return f"{summary.text()}\n{ready} ready to review: /pending"

    def screen(self, args: str = "", progress: Callable[[str], None] | None = None) -> str:
        return self._screen(args, progress)[0]

    def screen_replies(self, args: str = "",
                       progress: Callable[[str], None] | None = None,
                       limit: int | None = None) -> list[Reply]:
        """The screening summary, then an alert card for each new Apply high job. `limit`
        (a /screen choice button) screens that many now, over today's limit if needed."""
        word = args.strip()
        if word and word.lower() not in ("batch", "collect") and not _job_ref(word):
            return [Reply(SCREEN_USAGE)]
        text, summary = self._screen(args, progress, limit)
        return [Reply(text), *self.high_alerts(summary)]

    def screen_choice(self) -> Reply | None:
        """/screen with more jobs waiting than today's limit leaves (Phase 9 P9-19: it stopped
        silently at 30 with 109 waiting): ask first. None: run as usual."""
        if self.repo is None:
            return None
        from jobengine.screen import daily

        waiting = len(unscreened_rows(self.deps, self.today()))
        cap = daily.daily_limit(self.s)
        room = max(0, cap - daily.used_today(self.state, self.today()))
        if waiting <= room:
            return None
        buttons = []
        if room:
            buttons.append((f"Today's {room}", f"sc:{room}"))
        more = min(waiting, room + cap) if room else min(waiting, cap)
        if more > room:
            buttons.append((f"{more} (over the limit)", f"sc:{more}"))
        if waiting > more:
            buttons.append((f"All {waiting}", f"sc:{waiting}"))
        lines = [f"{waiting} jobs wait to be screened. Today's limit: {cap} a day, {room} left "
                 f"({cap - room} screened today)."]
        if room:
            lines.append(f"Today's {room}: the best {room} now, the rest wait for tomorrow.")
        lines.append(f"Over the limit: about ${SCREEN_COST_PER_JOB:.3f} a job (Haiku), so all "
                     f"{waiting} cost about ${waiting * SCREEN_COST_PER_JOB:.2f}. Jobs without "
                     "a description cost nothing (they wait for /jd).")
        return Reply("\n".join(lines), buttons)

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

    def _screen(self, args: str, progress: Callable[[str], None] | None,
                limit: int | None = None) -> tuple[str, ScreenSummary | None]:
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
                summary = screen_pending(self.s, self.deps, self.today(), progress, limit)
        except http.HttpError as exc:
            if exc.status == 400 and word not in ("batch", "collect"):  # not a page id
                return f"No job found for {args.strip()}.\n{SCREEN_USAGE}", None
            return f"Screening could not run: {exc}", None
        except (ScreenError, LLMError) as exc:
            return f"Screening could not run: {exc}", None
        # Rows with no description wait for /jd, whatever their board (8 Oct).
        waiting.note(self.state, (r.page_id for r in summary.results
                                  if r.verdict == "Unscreened"
                                  and r.description_kind == "none"), self.today())
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

    def check_scheduled(self, fetch: autopilot.Fetch | None,
                        progress: Callable[[str], None] | None = None
                        ) -> list[Reply] | None:
        """The afternoon check (screen/check.py): /fetch and /screen at Config
        schedule.check_times when Config schedule.check is true."""
        from jobengine.screen import check

        return check.scheduled(self, fetch, progress or (lambda text: None))

    def autopilot_when(self) -> list[Reply]:
        """/autopilot when: the schedule and the next run, and the afternoon check."""
        from jobengine.screen import check, schedule

        return [Reply(f"{schedule.status_text(self)}\n\n{check.status_text(self)}")]

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

    # ------------------------------------------------------------ LinkedIn jobs (1 Oct idea)

    def linkedin_rows(self) -> list[JobRow]:
        """LinkedIn jobs that wait for their description: new, never screened."""
        assert self.repo is not None
        rows = [r for r in waiting_for_jd(self.repo)
                if r.board == "LinkedIn" and r.status in (None, "", "New")]
        rows.sort(key=lambda r: r.swept_date or date.min, reverse=True)
        return rows

    def linkedin_command(self, index: int = 0) -> list[Reply]:
        """/linkedin: the LinkedIn jobs one at a time. Each one opens a /jd capture, so you
        only paste the description and tap Done (saved, screened, then its card)."""
        if self.repo is None:
            return [Reply(NO_TARGET)]
        rows = self.linkedin_rows()
        if not rows:
            self.captures.clear()
            return [Reply("No LinkedIn jobs wait for a description. They come from the "
                          "LinkedIn alert emails on each /fetch.")]
        i = index % len(rows)
        row = rows[i]
        self.captures.start(row.url or "", self.now(), row.page_id)
        place = ", ".join(p for p in (row.city, row.country) if p) or "place unknown"
        text = (f"LinkedIn job {i + 1} of {len(rows)}:\n{row.company}, {row.role}\n{place}\n"
                f"{short_link(row.url) or '(no link)'}\n\n{LINKEDIN_PASTE}")
        pid = short_id(row.page_id)
        buttons = [("Done", "li:done"), ("Next", f"li:next:{i + 1}"),
                   ("Not interested", f"li:skip:{pid}")]
        return [Reply(text, buttons)]

    def linkedin_tap(self, arg: str) -> list[Reply]:
        action, _, rest = arg.partition(":")
        if action == "done":
            replies = self.done()
            left = len(self.linkedin_rows()) if self.repo is not None else 0
            if left:
                replies.append(Reply(f"{left} more LinkedIn job(s) wait for a description.",
                                     [("Next LinkedIn job", "li:next:0")]))
            return replies
        if action == "skip" and rest and self.repo is not None:
            from jobengine.resume.builder import notion_id

            self.captures.clear()
            self.repo.update(notion_id(rest), {"Status": "Declined", "Skip reason": "Other"})
            return [Reply("Skipped that LinkedIn job."), *self.linkedin_command(0)]
        index = int(rest) if rest.isdigit() else 0
        return self.linkedin_command(index)

    def waiting_list(self) -> Reply:
        assert self.repo is not None
        rows = waiting_for_jd(self.repo, self.state)
        if not rows:
            return Reply("No jobs are waiting for a description. Use /jd <url> to paste one.")
        # Newest first within each board, LinkedIn first (8 Oct: other boards are listed too).
        rows.sort(key=lambda r: r.swept_date or date.min, reverse=True)
        rows.sort(key=lambda r: (r.board != "LinkedIn", r.board or ""))
        jobs = f"{len(rows)} jobs wait" if len(rows) > 1 else "1 job waits"
        lines = [f"{jobs} for a description ({_by_board(rows)}). Open the link, copy the job "
                 "text, then send /jd <url> and paste it:"]
        # Every board gets its share of the list (9 Oct: 28 LinkedIn jobs filled all 25 places
        # and the Pracuj.pl and IrishJobs links never showed).
        groups: dict[str, list[JobRow]] = {}
        for row in rows:
            groups.setdefault(row.board or "Other", []).append(row)
        share = max(MIN_PER_BOARD, MAX_WAITING_LIST // len(groups))
        for board, group in groups.items():
            lines.append(f"\n{board}:")
            for row in group[:share]:
                lines.append(f"- {_one_line(row.company)}, {_one_line(row.role)}\n"
                             f"  {row.url or '(no URL)'}")
            if len(group) > share:
                lines.append(f"  ... {len(group) - share} more {board} jobs")
        return Reply("\n".join(lines))

    def text(self, message: str) -> list[Reply] | None:
        """A plain text message. None when no capture is active (the bot answers as usual)."""
        capture = self.captures.get()
        if capture is None:
            return self.domain_text(message) or self._rebuild_answer(message)
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

    # ------------------------------------------------------------ /add <job link> (23 Oct)

    def add_anyway(self, job_ref: str) -> list[Reply]:
        """/add, then "Build the resume anyway": your choice wins over the Skip verdict; the
        Skip reason stays on the row and the verdict becomes Needs review."""
        row = find_row(self.repo, job_ref) if self.repo is not None else None
        if row is None:
            return [Reply("That job is no longer in Job Opportunities.")]
        self.repo.update(row.page_id, {"Screen verdict": "Needs review", "Status": "Approved"})
        return self.on_job_approved(row.page_id, self.repo.get_values(row.page_id) or {})

    def _read_add_page(self, url: str) -> tuple[str, str]:
        """The /add page, waiting up to ADD_TIMEOUT, with one more try after a time-out."""
        try:
            return self.get_slow_page(url)
        except http.HttpError as exc:
            if "Timeout" not in str(exc):
                raise
            log.info("/add: %s timed out, trying once more", http.safe_url(url))
            return self.get_slow_page(url)

    def add_command(self, args: str) -> list[Reply]:
        """/add <job link>: the company's own job page (LinkedIn and other apps usually send
        you there). Reads it, saves and screens the job, adds the company to Target
        Companies, then builds the resume (you apply to it, so it is Approved)."""
        if self.repo is None:
            return [Reply(NO_TARGET)]
        url, _ = jd_capture.parse_jd_command(args)
        if url is None:
            return [Reply(ADD_USAGE)]
        host = (urlsplit(url).hostname or "").lower()
        if "linkedin" in host or http.host_in(host, blocked_hosts(self.s)):
            return [Reply(ADD_BLOCKED.format(host=host, url=url))]
        self.say(Reply("Reading the job page..."))
        try:
            final, page = self._read_add_page(url)
        except http.HttpError as exc:
            return [Reply(f"Could not open the job page ({exc}).\n{ADD_PASTE.format(url=url)}")]
        job = add_link.read_page(final, page)
        if not job.description or not job.title or not job.company:
            missing = "description" if not job.description else "company or role"
            return [Reply(f"The page does not show the job's {missing} in a way the bot can "
                          f"read.\n{ADD_PASTE.format(url=url)}")]
        rules = Rules.from_config(self.s.sweep)
        country, city = place(job.location, rules)
        if country is None and job.description:
            # Phase 13 B-03: no place in the page's data (Comarch): read it from the text.
            country, city = place(job.description[:ADD_PLACE_CHARS], rules)
        if country not in add_link.COUNTRIES:
            country, city = None, None
        row = self._find(final) or self._find(url)
        page_id = row.page_id if row else self._add_row(job, final, country, city)
        size = int(self.s.screening.get("block_chars", 2000))
        limit = int(self.s.screening.get("max_blocks", 90))
        text = job.description
        # A row from an alert has only a snippet: the company page's text is newer and wins
        # (description() reads the newest), so its "minimum 5 years" counts (Phase 13 B-03).
        old, kind = description(self.repo.read_body(page_id)) if row else ("", "none")
        if text.strip() != old.strip() and (
                kind != "full" or len(text) > len(old) + ADD_LONGER_CHARS):
            self.repo.append_body(page_id, ["Description source: /add (full)",
                                            *[text[i:i + size] for i in range(0, len(text),
                                                                               size)][:limit]])
        where = ", ".join(p for p in (city, country) if p) or job.location or "place not given"
        replies = [Reply(f"{'Found' if row else 'Saved'}: {job.company}, {job.title} ({where})."
                         " Screening it now...")]
        if job.company_from_link:
            replies.append(Reply(f"(The page does not name the company: \"{job.company}\" is "
                                 "taken from the link. Fix the Company field in Notion if it "
                                 "is wrong.)"))
        if not rules.remote_jobs and remote_only(job.location, job.title, city):
            replies.append(Reply("Note: this job looks remote (you want office or hybrid). "
                                 "It is saved because you chose it."))
        replies.append(Reply(self.screen(page_id)))
        replies.extend(self._add_company(job.company, final, country))
        values = self.repo.get_values(page_id) or {}
        if values.get("Screen verdict") == "Skip":
            ref = short_id(page_id)
            replies.append(Reply(
                f"Screening says Skip: {values.get('Skip reason') or 'no reason given'}. No "
                "resume yet. You chose this job: build it anyway, or leave it.",
                [("Build the resume anyway", f"aa:{ref}"), ("Not applying", f"na:{ref}")]))
            return replies
        self.repo.update(page_id, {"Status": "Approved"})
        replies.extend(self.on_job_approved(page_id, values))
        return replies

    def _add_row(self, job: add_link.PageJob, url: str, country: str | None,
                 city: str | None) -> str:
        assert self.repo is not None
        today = self.today()
        plan: dict[str, Any] = {
            "Company": job.company, "Role": job.title, "URL": url, "Board": "Company site",
            "Status": "New", "Screen verdict": "Unscreened", "First seen": today,
            "Swept date": today, "Times seen": 1,
            "Dedupe key": dedupe_key(job.company, job.title, city, country or ""),
        }
        if country:
            plan["Country"] = country
        if city:
            plan["City"] = city
        return self.repo.create(plan, [])

    def _add_company(self, company: str, url: str, country: str | None,
                     note: str | None = None) -> list[Reply]:
        """The job's company into Target Companies, unless it is there or an agency."""
        known = add_link.known_company(company, [c.name for c in self.reference().companies])
        if known:
            return [Reply(f"{known} is already in Target Companies.")]
        if agency_by_name(company, self.deps.config()):
            return [Reply(f"{company} looks like a recruitment agency: not added to Target "
                          "Companies.")]
        if self.companies is None:
            return [Reply(f"{company} is not in Target Companies; it cannot be added from "
                          "here (no Notion access).")]
        props = add_link.company_props(company, url, country, self.today(), note)
        page_id = self.companies.add(props)
        self._reference = None  # read Target Companies again next time
        board = props["ATS platform"]
        if not props.get("Careers URL"):
            how = "no link yet (the next /fetch looks for its job board by the name)"
        elif board not in ("Custom", "Unknown"):
            how = f"its {board} job board: {props['Careers URL']}"
        else:
            how = f"its site (the job board is found on the next /fetch): {props['Careers URL']}"
        return [Reply(f"Added {company} to Target Companies with {how}\nEvery /fetch checks "
                      "it from now on.", [("Undo", f"tu:{short_id(page_id)}")])]

    # ------------------------------------------------------------ /newcompanies (C1)

    def newcompanies_command(self, args: str = "") -> list[Reply]:
        """New companies hiring for your roles (sweep/discover.py), one at a time."""
        from jobengine.sweep import discover

        items = discover.suggestions(self.state, self.reference().companies)
        if not items:
            return [Reply("No new companies to suggest. After each /fetch, companies with jobs "
                          "for your roles that are not in Target Companies show up here.")]
        key, item = items[0]
        url, platform = discover.site_of(item.get("urls") or [])
        roles = item.get("roles") or []
        shown = "; ".join(roles[:discover.MAX_ROLES])
        more = f" and {len(roles) - discover.MAX_ROLES} more" if len(roles) > discover.MAX_ROLES \
            else ""
        if url and platform not in ("Unknown", "Custom"):
            site = f"Job board: {platform}, {url} (every /fetch can read it)"
        elif url:
            site = f"Site: {url} (the next /fetch looks for its job board there)"
        else:
            site = "Site: not known yet (the next /fetch looks for its job board by the name)"
        ref = discover.ref(key)
        return [Reply(
            f"\U0001F50E New company 1 of {len(items)}: {item.get('name')}\n"
            f"Jobs seen ({', '.join(item.get('countries') or [])}): {shown}{more}\n"
            f"Found through: {', '.join(item.get('boards') or [])}\n{site}\n"
            "Add it to Target Companies, so every /fetch reads its own job board?",
            [("Add to Target Companies", f"dc:{ref}"), ("Not interested", f"dn:{ref}")])]

    def newcompany_tap(self, action: str, short: str) -> list[Reply]:
        from jobengine.sweep import discover

        found = discover.find_ref(self.state, short)
        if found is None:
            return [Reply("That suggestion is no longer open."), *self.newcompanies_command()]
        key, item = found
        if action == "dn":
            discover.drop(self.state, key, never_again=True)
            return [Reply(f"OK: {item.get('name')} is not suggested again."),
                    *self.newcompanies_command()]
        url, _platform = discover.site_of(item.get("urls") or [])
        country = next((c for c in item.get("countries") or [] if c in add_link.COUNTRIES), None)
        note = (f"Added by /newcompanies on {self.today().isoformat()}: jobs seen through "
                f"{', '.join(item.get('boards') or [])}")
        replies = self._add_company(item.get("name") or "", url, country, note)
        discover.drop(self.state, key)
        return [*replies, *self.newcompanies_command()]

    def add_undo(self, arg: str) -> list[Reply]:
        from jobengine.resume.builder import notion_id

        if self.companies is None:
            return [Reply("Target Companies cannot be changed from here.")]
        self.companies.deactivate(notion_id(arg))
        self._reference = None
        return [Reply("Undone: the company stays in Target Companies with Active unticked, "
                      "so /fetch no longer checks it.")]

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

    def no_file(url: str) -> tuple[str, str, bytes]:
        raise http.HttpError(f"GET {url} failed: no network in --fake")

    desk.get_file = no_file
    desk.out_dir = ROOT_DIR / "out" / "fake"  # never your real out/ie_permits file
    desk.table_query = lambda source: {"results": []}  # no Notion in --fake
    desk.skills_added = []  # type: ignore[attr-defined]
    desk.add_skill = lambda name, level, note: desk.skills_added.append(  # type: ignore[attr-defined]
        (name, level, note))
    desk.connections = connections.FakeConnections()
    from jobengine.notion_repo import FakeCompanyAdder

    desk.companies = FakeCompanyAdder()
    desk.mails = []  # type: ignore[attr-defined]  # (subject, text) of each mail to you
    from jobengine.notion_repo import FakeTable

    desk.answer_bank, desk.autoapply_issues = FakeTable(), FakeTable()
    desk.pings = []  # type: ignore[attr-defined]  # heartbeat pings: True ok, False fail
    desk.heartbeat = lambda ok: desk.pings.append(ok) or True  # type: ignore[attr-defined]
    desk.owner_mail = lambda subject, text: desk.mails.append(  # type: ignore[attr-defined]
        (subject, text))
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
    desk = Desk(s, real_deps(s), state, resume=resume, contacts=contacts, mail=mail,
                track=track, strategy=strategy)
    # What the bot did today, by your clock (tracking.utc_offset), for the daily report.
    day_ledger.bind(state, lambda: desk.tracking_now().date(), desk.tracking_now)
    from jobengine.notion_repo import skill_writer

    desk.add_skill = skill_writer(client, s)
    desk.connections = connections.store_for(s, client)
    from jobengine.notion_repo import company_adder

    desk.companies = company_adder(s, client)
    from jobengine.notion_repo import autoapply_tables

    desk.answer_bank, desk.autoapply_issues = autoapply_tables(s, client)
    return desk
