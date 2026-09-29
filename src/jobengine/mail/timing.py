"""Send at the recipient's morning (PR 13), mail mode "send" only.

A mail that passed every check is sent at once only inside the recipient's send window
(default Tuesday to Thursday, 08:00 to 10:00 in the job's country). Outside it, the draft
stays in Gmail and waits in a queue (Bot State `mail.queue`); the queue is checked every few
minutes (long polling, or Cloud Scheduler's POST /tasks/mail on Cloud Run) and sends at
most one waiting mail per check, so the mails of a morning go out a few minutes apart.

At send time the draft is checked again exactly as before (Gmail's copy, the approved
resume, To, subject, signature) and the daily send cap still applies. A draft you sent or
deleted yourself leaves the queue. `mail.send_window.enabled: false` in the yaml (or Config
`mail.send_window` = `off`) sends at once, as before PR 13.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from jobengine.bot_state import BotState

QUEUE_KEY = "mail.queue"  # bot_state: {"items": [QueueItem as dict, ...]}
WINDOW_KEY = "mail.send_window"  # Notion Config: "off" sends at once
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
ZONES = {"Poland": "Europe/Warsaw", "Netherlands": "Europe/Amsterdam",
         "Ireland": "Europe/Dublin"}
DEFAULT_ZONE = "Europe/Warsaw"
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Window:
    enabled: bool
    days: tuple[int, ...]  # 0 = Monday
    start: time
    end: time

    def text(self) -> str:
        names = [DAYS[d] for d in self.days]
        span = (f"{names[0]} to {names[-1]}" if len(names) > 2 and
                list(self.days) == list(range(self.days[0], self.days[-1] + 1))
                else ", ".join(names))
        return f"{span}, {self.start:%H:%M} to {self.end:%H:%M} the recipient's time"


@dataclass
class QueueItem:
    draft_id: str
    job_id: str
    page_id: str  # the contact
    name: str
    to: str  # after safety.route_recipients
    subject: str
    template: str
    thread_id: str
    country: str
    signature: str  # the signature's first line, checked again at send time
    due: str  # ISO time (UTC) of the next window start

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> QueueItem:
        return cls(**{k: str(value.get(k) or "") for k in cls.__dataclass_fields__})


def _time(value: object, default: str) -> time:
    match = TIME_RE.match(str(value or "").strip()) or TIME_RE.match(default)
    assert match is not None
    return time(int(match.group(1)), int(match.group(2)))


def window(mail_yaml: dict[str, Any], config: Any) -> Window:
    raw = mail_yaml.get("send_window") or {}
    items = raw.get("days") or ["Tue", "Wed", "Thu"]
    days = tuple(sorted({DAYS.index(str(d)[:3].title()) for d in items
                         if str(d)[:3].title() in DAYS})) or (1, 2, 3)
    start, end = _time(raw.get("start"), "08:00"), _time(raw.get("end"), "10:00")
    enabled = bool(raw.get("enabled", True))
    if (config.get(WINDOW_KEY) or "").strip().lower() in ("off", "false", "no"):
        enabled = False
    return Window(enabled=enabled, days=days, start=start, end=max(start, end))


def zone(country: str | None) -> ZoneInfo:
    return ZoneInfo(ZONES.get((country or "").strip(), DEFAULT_ZONE))


def aware(now: datetime) -> datetime:
    """A naive clock is the machine's own local time."""
    return now if now.tzinfo is not None else now.astimezone()


def inside(w: Window, country: str | None, now: datetime) -> bool:
    local = aware(now).astimezone(zone(country))
    return local.weekday() in w.days and w.start <= local.time() < w.end


def next_start(w: Window, country: str | None, now: datetime) -> datetime:
    """The next time the window opens for this country (now, when it is open)."""
    tz = zone(country)
    local = aware(now).astimezone(tz)
    if inside(w, country, now):
        return local
    for ahead in range(8):
        day = local.date() + timedelta(days=ahead)
        at = datetime.combine(day, w.start, tzinfo=tz)
        if day.weekday() in w.days and at > local:
            return at
    return local  # pragma: no cover: some day in the next week is always a window day


def when_text(at: datetime) -> str:
    return f"{at:%a %d %b %H:%M} {at.tzname()}"


# ---------------------------------------------------------------- the queue


def load(state: BotState | None) -> list[QueueItem]:
    value = (state.get(QUEUE_KEY) or {}) if state is not None else {}
    return [QueueItem.from_dict(v) for v in value.get("items") or [] if isinstance(v, dict)]


def save(state: BotState | None, items: list[QueueItem]) -> None:
    if state is not None:
        state.set(QUEUE_KEY, {"items": [asdict(i) for i in items]})


def add(state: BotState | None, item: QueueItem) -> None:
    items = [i for i in load(state) if i.draft_id != item.draft_id]
    save(state, [*items, item])


def due_items(state: BotState | None, now: datetime, w: Window) -> list[QueueItem]:
    """Waiting mails whose window is open now, the longest waiting first."""
    stamp = aware(now)
    items = [i for i in load(state)
             if datetime.fromisoformat(i.due) <= stamp and
             (not w.enabled or inside(w, i.country, now))]
    return sorted(items, key=lambda i: i.due)


def remove(state: BotState | None, draft_id: str) -> None:
    save(state, [i for i in load(state) if i.draft_id != draft_id])
