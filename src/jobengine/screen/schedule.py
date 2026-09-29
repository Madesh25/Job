"""Scheduled /autopilot (PR 12): every morning, European time, without a command.

It is off until Notion Config `schedule.autopilot` is `true`. When on, the first check at or
after the start time (default 07:00 Europe/Warsaw, Monday to Friday) runs /autopilot once for
that day; a check after the latest start time (default 10:00) does not start it any more, so
a bot that was off in the morning does not run it in the afternoon.

- Long polling (the bot on your laptop): the loop checks every few minutes.
- Webhook (Cloud Run): Cloud Scheduler calls POST /tasks/autopilot every 30 minutes in the
  morning; the first call starts the run, later calls carry on with its half-price batch.

The run is the same as sending /autopilot: approvals are capped by
`screening.autopilot_approvals` a day, and mail mode decides between drafts and sending.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from jobengine.screen.desk import Desk

SWITCH_KEY = "schedule.autopilot"  # Notion Config: "true" turns the schedule on
TIME_KEY = "schedule.autopilot_time"  # Notion Config: optional start time, "HH:MM"
LAST_KEY = "autopilot.scheduled"  # bot_state: {"date": <local iso date of the last start>}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULT_TIME = "07:00"
DEFAULT_LATEST = "10:00"
DEFAULT_ZONE = "Europe/Warsaw"
HOME_ZONE = "Asia/Kolkata"  # the status line also shows the time in India
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Plan:
    on: bool
    start: time
    latest: time
    zone: ZoneInfo
    days: tuple[int, ...]  # 0 = Monday

    def days_text(self) -> str:
        names = [DAYS[d] for d in self.days]
        if list(self.days) == list(range(self.days[0], self.days[-1] + 1)) and len(names) > 2:
            return f"{names[0]} to {names[-1]}"
        return ", ".join(names)


def _time(value: object, default: str) -> time:
    match = TIME_RE.match(str(value or "").strip())
    if not match:
        match = TIME_RE.match(default)
        assert match is not None
    return time(int(match.group(1)), int(match.group(2)))


def _days(value: object) -> tuple[int, ...]:
    items = value if isinstance(value, list) else str(value or "").replace(",", " ").split()
    picked = sorted({DAYS.index(str(d)[:3].title()) for d in items
                     if str(d)[:3].title() in DAYS})
    return tuple(picked) or tuple(range(5))


def plan(desk: Desk) -> Plan:
    """The schedule: yaml `screening.autopilot_schedule`, with Notion Config on top."""
    base = desk.s.screening.get("autopilot_schedule") or {}
    config = desk.deps.config()
    on = (config.get(SWITCH_KEY) or "false").strip().lower() == "true"
    start = _time(config.get(TIME_KEY) or base.get("time"), DEFAULT_TIME)
    latest = _time(base.get("latest"), DEFAULT_LATEST)
    if latest < start:
        latest = start
    return Plan(on=on, start=start, latest=latest,
                zone=ZoneInfo(str(base.get("timezone") or DEFAULT_ZONE)),
                days=_days(base.get("days")))


def local_now(desk: Desk, p: Plan) -> datetime:
    """Now in the schedule's time zone. A naive clock is the machine's own local time."""
    now = desk.now()
    return (now if now.tzinfo is not None else now.astimezone()).astimezone(p.zone)


def started_today(desk: Desk, p: Plan) -> bool:
    last = desk.state.get(LAST_KEY) or {}
    return last.get("date") == local_now(desk, p).date().isoformat()


def due(desk: Desk) -> bool:
    """True when the schedule is on and today's run has not started yet (within the window)."""
    p = plan(desk)
    if not p.on:
        return False
    now = local_now(desk, p)
    if now.weekday() not in p.days or not p.start <= now.time() <= p.latest:
        return False
    return not started_today(desk, p)


def mark_started(desk: Desk) -> None:
    """Before the run: a failed run is not started again that day (send /autopilot)."""
    p = plan(desk)
    desk.state.set(LAST_KEY, {"date": local_now(desk, p).date().isoformat()})


def next_start(desk: Desk, p: Plan) -> datetime:
    now = local_now(desk, p)
    for ahead in range(8):
        day = (now + timedelta(days=ahead)).date()
        at = datetime.combine(day, p.start, tzinfo=p.zone)
        if day.weekday() not in p.days:
            continue
        if ahead == 0 and (started_today(desk, p) or now.time() > p.latest):
            continue
        return at
    return datetime.combine(now.date(), p.start, tzinfo=p.zone)  # pragma: no cover


def status_text(desk: Desk) -> str:
    """/autopilot when: whether the schedule is on, and the next run."""
    p = plan(desk)
    zone = p.zone.key
    window = (f"{p.days_text()} at {p.start:%H:%M} {zone} (a bot that is off then starts "
              f"it until {p.latest:%H:%M})")
    if not p.on:
        return ("Scheduled autopilot: off. To run /autopilot every morning by itself, set "
                f"Notion Config {SWITCH_KEY} to true. It would run {window}.")
    at = next_start(desk, p)
    home = at.astimezone(ZoneInfo(HOME_ZONE))
    ran = " Today's run has started." if started_today(desk, p) else ""
    return (f"Scheduled autopilot: on, {window}.{ran} Next: {at:%a %d %b %H:%M} {zone} "
            f"({home:%H:%M} in India). Turn it off with Notion Config {SWITCH_KEY} = false.")
