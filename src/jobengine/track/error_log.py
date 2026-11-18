"""Error Log (18 Nov, your request): one Notion row per kind of problem per day, so the days
after go-live can be looked back on and the problems fixed by Claude Code ("read the Error
Log rows with Status New and fix them").

What counts:
- every ERROR and WARNING line of the bot's own loggers;
- the site problems the bot logs as INFO because the run goes on: a site that refuses the
  bot (403), asks it to slow down (429), a page that is gone (404/410), a timeout, a server
  error. These are Level "Site problem".

Lines of the same kind are one problem: an error by its message (links, ids and numbers
blanked), a warning or a site problem by the place in the code that logged it plus its likely
cause, so "careers page not read" for 30 sites is one row with Count 30 and the sites in
Example. Each row has its first and last time, count, stage, level, likely cause (a fixed map
of the message), where it ran and one example line (emails, keys, tokens and link queries
blanked).

The lines are gathered in memory and written at the end of every scheduled run (Run Log,
track/run_log.py), so a short Cloud Run task loses nothing: the rows of the last 8 days are
read once, today's row of each problem gets the new count, a new one is added with Status
New. Your Status, Notes and Fix PR are never overwritten.

A new kind of error or warning (no row in the last 7 days) is sent to Telegram once, with
the others of the same run in one message. Site problems never ping: they come every day.
Writing the rows or the message never fails the run.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from jobengine.track.run_log import bump, redact, where

log = logging.getLogger("jobengine.error_log")

LOOK_BACK_DAYS = 7  # a problem with no row in these days is new
MAX_PROBLEMS = 200  # kinds kept in memory between writes
MAX_SITES = 8  # sites named in Example
PING_LINES = 5
TITLE_CHARS = 150
EXAMPLE_CHARS = 900

LEVEL_ERROR, LEVEL_WARNING, LEVEL_SITE = "Error", "Warning", "Site problem"

_SITE = re.compile(
    r"HTTP [45]\d\d|\b(?:401|403|404|410|429|50[0-4])\b|rate limited: every|refused the bot"
    r"|blocks the bot|\bbusy\b|timed? ?out|Timeout|ConnectError|ConnectionError", re.I)
_NOT_SITE = re.compile(r"retrying|dry run", re.I)

# Likely cause: the first that matches the message (and the exception's name).
CAUSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Rate limited (429)", re.compile(r"\b429\b|rate limit|too many requests", re.I)),
    ("Key expired or wrong (401)", re.compile(
        r"\b401\b|invalid_grant|unauthori[sz]ed|invalid (?:api )?key|api key|token has been "
        r"expired|token expired|authentication", re.I)),
    ("Refuses the bot (403)", re.compile(
        r"\b403\b|forbidden|refused the bot|blocks the bot|access denied", re.I)),
    ("Page gone (404)", re.compile(r"\b(?:404|410)\b|not found|page gone", re.I)),
    ("Timeout", re.compile(r"timed? ?out|timeout|deadline", re.I)),
    ("Server error (5xx)", re.compile(
        r"\b50[0-4]\b|server error|bad gateway|service unavailable|overloaded", re.I)),
    ("Network", re.compile(
        r"connect(?:ion)?(?:error| refused| reset| aborted)|network|name resolution|"
        r"getaddrinfo|ssl|remotedisconnected|unreachable", re.I)),
    ("Bot bug", re.compile(
        r"traceback|\b(?:Type|Key|Attribute|Value|Index|Name|Assertion|ZeroDivision|"
        r"UnboundLocal)Error\b|NoneType", re.I)),
)

# Stage by the module that logged the line (its path under jobengine/).
STAGES: tuple[tuple[str, str], ...] = (
    ("sweep/job_links", "Job links"), ("sweep/", "Fetch"), ("http", "Fetch"),
    ("ind_register", "Fetch"), ("ie_permits", "Fetch"),
    ("screen/", "Screen"), ("jdtrim", "Screen"), ("resume/", "Resume"),
    ("apply/", "Apply"), ("contacts/", "Contacts"), ("outreach/", "Contacts"),
    ("mail/", "Mail"), ("gmail", "Mail"), ("track/", "Tracking"), ("strategy/", "Tracking"),
    ("telegram_bot", "Telegram"), ("web/", "Telegram"), ("notion", "Notion"),
    ("bot_state", "Notion"), ("config_store", "Notion"), ("llm", "AI"),
)

_URL = re.compile(r"https?://[^\s'\")]+")
_HEX = re.compile(r"\b[0-9a-f]{8}(?:-?[0-9a-f]{4}){3}-?[0-9a-f]{12}\b|\b[0-9a-f]{16,}\b", re.I)
_NUM = re.compile(r"(?<!HTTP )\b\d+(?:\.\d+)?\b")
_HOST = re.compile(r"(?<![\w.@-])(?:[a-z0-9-]+\.)+[a-z]{2,10}(?![\w.-])")


def cause_of(text: str) -> str:
    for name, rule in CAUSES:
        if rule.search(text):
            return name
    return "Other"


def stage_of(record: logging.LogRecord) -> str:
    path = (record.pathname or "").replace("\\", "/")
    tail = path.split("jobengine/", 1)[-1] if "jobengine/" in path else record.name
    for part, stage in STAGES:
        if part in tail:
            return stage
    return "Other"


def _message(record: logging.LogRecord) -> str:
    try:
        text = record.getMessage()
    except Exception:  # a bad format string still counts
        text = str(record.msg)
    if record.exc_info and record.exc_info[1] is not None:
        exc = record.exc_info[1]
        text = f"{text}: {type(exc).__name__}: {exc}"
    return text


def _blank(text: str) -> str:
    text = _NUM.sub("N", _HEX.sub("<id>", _URL.sub("<link>", text)))
    return " ".join(text.split())


def sites_in(text: str) -> list[str]:
    hosts = [urlsplit(url).hostname or "" for url in _URL.findall(text)]
    hosts += _HOST.findall(_URL.sub(" ", text).lower())
    return [h for h in dict.fromkeys(hosts) if h and not h.endswith((".py", ".json", ".yaml"))]


def level_of(record: logging.LogRecord, text: str) -> str | None:
    if record.levelno >= logging.ERROR:
        return LEVEL_ERROR
    if record.levelno >= logging.WARNING:
        return LEVEL_WARNING
    if record.levelno >= logging.INFO and _SITE.search(text) and not _NOT_SITE.search(text):
        return LEVEL_SITE
    return None


@dataclass
class Problem:
    """One kind of problem since the last write."""

    pattern: str
    title: str
    stage: str
    level: str
    cause: str
    example: str
    first: float
    last: float
    count: int = 0
    sites: list[str] = field(default_factory=list)

    def digest(self) -> str:
        return hashlib.sha1(self.pattern.encode()).hexdigest()[:12]

    def example_text(self) -> str:
        text = self.example
        if self.sites:
            text += "\nSites: " + ", ".join(self.sites[:MAX_SITES])
            if len(self.sites) > MAX_SITES:
                text += f" (+{len(self.sites) - MAX_SITES})"
        return text[:EXAMPLE_CHARS]


def problem_of(record: logging.LogRecord) -> Problem | None:
    if not record.name.startswith("jobengine") or record.name == log.name:
        return None
    text = _message(record)
    level = level_of(record, text)
    if level is None:
        return None
    first_line = text.splitlines()[0] if text else ""
    cause = cause_of(text)
    stage = stage_of(record)
    if level == LEVEL_ERROR:  # rare: the message itself tells the problems apart
        shape = _blank(first_line)
    else:  # the line in the code that logged it, and why
        shape = _blank(str(record.msg).replace("%s", "…").replace("%r", "…")
                       .replace("%d", "N").splitlines()[0])
    pattern = f"{level}|{stage}|{cause}|{shape[:200]}"
    title = redact(_blank(first_line) if level == LEVEL_ERROR else shape)[:TITLE_CHARS]
    created = record.created
    return Problem(pattern, title or cause, stage, level, cause,
                   redact(first_line)[:EXAMPLE_CHARS], created, created, 0,
                   sites_in(redact(text)))


_lock = threading.Lock()
_write_lock = threading.Lock()
_pending: dict[str, Problem] = {}
_local = threading.local()
_target: dict[str, Any] = {"table": None, "now": None, "notify": None}


class Collector(logging.Handler):
    """Gathers the bot's problems in memory until the next write."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_local, "writing", False):  # the write's own Notion lines
            return
        try:
            problem = problem_of(record)
        except Exception:  # never break the logging of the bot
            return
        if problem is None:
            return
        if problem.level == LEVEL_SITE and problem.cause in ("Rate limited (429)",
                                                             "Refuses the bot (403)"):
            bump("sites_blocked")
        with _lock:
            have = _pending.get(problem.pattern)
            if have is None:
                if len(_pending) >= MAX_PROBLEMS:
                    return
                have = _pending[problem.pattern] = problem
            have.count += 1
            have.last = max(have.last, problem.last)
            for site in problem.sites:
                if site not in have.sites:
                    have.sites.append(site)


def install(root: logging.Logger | None = None) -> Collector:
    """Add the collector once (the bot and the web service call this at start)."""
    root = root or logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, Collector):
            return handler
    collector = Collector()
    root.addHandler(collector)
    return collector


def bind(table: Any, now: Callable[[], datetime]) -> None:
    """Where the rows go (None: not writable here) and the bot's clock."""
    _target["table"], _target["now"] = table, now


def set_notify(notify: Callable[[str], None] | None) -> None:
    """How a new kind of error reaches you (Telegram)."""
    _target["notify"] = notify


def pending() -> int:
    with _lock:
        return sum(p.count for p in _pending.values())


def reset() -> None:
    """Tests only."""
    with _lock:
        _pending.clear()
    _target.update(table=None, now=None, notify=None)


def _key(day: date, digest: str) -> str:
    return f"{day.isoformat()}|{digest}"


def _when(stamp: float, zone: Any) -> datetime:
    return datetime.fromtimestamp(stamp, tz=zone).replace(microsecond=0)


def flush() -> int:
    """Write what was gathered; the number of problems written (0 when nothing was new or
    the Error Log is not writable here)."""
    table, now = _target["table"], _target["now"]
    if table is None or now is None:
        return 0
    with _lock:
        if not _pending:
            return 0
        batch = list(_pending.values())
        _pending.clear()
    with _write_lock:
        _local.writing = True
        try:
            return _write(table, now, batch)
        except Exception as exc:  # the Error Log never fails the run: keep it for later
            with _lock:
                for problem in batch:
                    have = _pending.setdefault(problem.pattern, problem)
                    if have is not problem:
                        have.count += problem.count
                        have.first = min(have.first, problem.first)
            log.info("error log not written: %s", redact(str(exc))[:200])
            return 0
        finally:
            _local.writing = False


def _write(table: Any, now: Callable[[], datetime], batch: list[Problem]) -> int:
    try:
        current = now()
    except Exception:  # a clock that needs Notion
        current = datetime.now().astimezone()
    zone = current.tzinfo or datetime.now().astimezone().tzinfo
    today = current.date()
    rows = table.recent(today - timedelta(days=LOOK_BACK_DAYS))
    by_key = {str(values.get("Key") or ""): (page_id, values) for page_id, values in rows}
    seen = {key.split("|", 1)[-1] for key in by_key}
    new_kinds: list[Problem] = []
    for problem in batch:
        day = _when(problem.first, zone).date()
        key = _key(day, problem.digest())
        last = _when(problem.last, zone)
        if key in by_key:
            page_id, values = by_key[key]
            table.update(page_id, {
                "Count": int(values.get("Count") or 0) + problem.count,
                "Last seen": last, "Example": problem.example_text(), "Where": where(),
            })
            continue
        table.create({
            "Problem": problem.title, "Day": day, "First seen": _when(problem.first, zone),
            "Last seen": last, "Count": problem.count, "Stage": problem.stage,
            "Level": problem.level, "Likely cause": problem.cause, "Where": where(),
            "Status": "New", "Example": problem.example_text(), "Key": key,
        })
        if problem.level != LEVEL_SITE and problem.digest() not in seen:
            new_kinds.append(problem)
    _ping(new_kinds)
    return len(batch)


def _ping(problems: list[Problem]) -> None:
    notify = _target["notify"]
    if not problems or notify is None:
        return
    lines = [f"\U0001F195 New kind of problem in the Error Log ({len(problems)}):"]
    for problem in problems[:PING_LINES]:
        lines.append(f"• {problem.level}, {problem.stage}: {problem.title} "
                     f"({problem.cause}, {problem.count}x)")
    if len(problems) > PING_LINES:
        lines.append(f"... and {len(problems) - PING_LINES} more.")
    lines.append("The rows are in Notion (Error Log, Status New): ask Claude Code to fix them "
                 "when you like.")
    try:
        notify("\n".join(lines))
    except Exception as exc:  # the message is a courtesy, the rows are written
        log.info("error log message not sent: %s", redact(str(exc))[:200])
