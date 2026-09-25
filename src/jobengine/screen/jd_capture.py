"""/jd capture (spec section 9.2): collect a pasted job description across several messages.

Telegram splits a long paste into several messages, so the text is collected in bot_state
(key `jd_capture`) until /done. The capture survives a bot restart. A capture older than
the window (20 minutes by default) is discarded when the next text arrives.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jobengine.bot_state import BotState
from jobengine.sweep.sources.gmail_alerts import board_for_sender

KEY = "jd_capture"
WINDOW_MINUTES = 20
MAX_CHARS = 170_000  # fits 90 blocks of 2000 characters and the 200k bot_state limit
FIELD_LINE = re.compile(r"^\s*(company|role|country|city)\s*:\s*(.+?)\s*$", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+")


@dataclass
class Capture:
    url: str
    started_at: str  # ISO datetime
    page_id: str | None = None
    chars: int = 0
    parts: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)  # company, role, country, city

    def started(self) -> datetime:
        return datetime.fromisoformat(self.started_at)

    def text(self) -> str:
        return "\n".join(self.parts).strip()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Capture:
        return cls(
            url=value.get("url") or "",
            started_at=value["started_at"],
            page_id=value.get("page_id"),
            chars=int(value.get("chars") or 0),
            parts=list(value.get("parts") or []),
            fields=dict(value.get("fields") or {}),
        )


def linkedin_posting_id(url: str) -> str | None:
    """"linkedin:<id>" for a LinkedIn job URL (/jobs/view/<id> or ?currentJobId=<id>)."""
    parts = urlsplit(url)
    if "linkedin" not in (parts.hostname or ""):
        return None
    match = re.search(r"/jobs/view/(?:[^/]*?-)?(\d{6,})", parts.path)
    if match:
        return f"linkedin:{match.group(1)}"
    current = parse_qs(parts.query).get("currentJobId")
    if current and current[0].isdigit():
        return f"linkedin:{current[0]}"
    return None


def board_for_url(url: str, sender_boards: dict[str, str]) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if "linkedin" in host:
        return "LinkedIn"
    return board_for_sender(f"jobs@{host}", sender_boards) if host else "Other"


def split_fields(text: str) -> tuple[dict[str, str], str]:
    """Leading "Company: ...", "Role: ...", "Country: ...", "City: ..." lines, and the rest."""
    found: dict[str, str] = {}
    lines = text.strip("\n").split("\n")
    while lines:
        match = FIELD_LINE.match(lines[0])
        if not match:
            break
        found[match.group(1).lower()] = match.group(2)
        lines.pop(0)
    return found, "\n".join(lines).strip()


def parse_jd_command(args: str) -> tuple[str | None, str]:
    """(url, text after the url) for "/jd <url> <optional text>"."""
    match = URL_RE.search(args)
    if not match:
        return None, args.strip()
    return match.group(0), args[match.end():].strip()


class Captures:
    """The one active capture, stored in bot_state."""

    def __init__(self, state: BotState, window_minutes: int = WINDOW_MINUTES):
        self.state = state
        self.window = timedelta(minutes=window_minutes)

    def get(self) -> Capture | None:
        value = self.state.get(KEY)
        if not value or "started_at" not in value:
            return None
        try:
            return Capture.from_dict(value)
        except (ValueError, TypeError):
            return None

    def expired(self, capture: Capture, now: datetime) -> bool:
        return now - capture.started() > self.window

    def start(self, url: str, now: datetime, page_id: str | None) -> Capture:
        capture = Capture(url=url, started_at=now.isoformat(timespec="seconds"), page_id=page_id)
        self.save(capture)
        return capture

    def add(self, capture: Capture, text: str) -> Capture:
        fields, rest = split_fields(text)
        capture.fields.update(fields)
        if rest:
            room = MAX_CHARS - capture.chars
            rest = rest[:max(room, 0)]
            if rest:
                capture.parts.append(rest)
                capture.chars += len(rest) + 1
        self.save(capture)
        return capture

    def save(self, capture: Capture) -> None:
        self.state.set(KEY, asdict(capture))

    def clear(self) -> None:
        self.state.delete(KEY)
