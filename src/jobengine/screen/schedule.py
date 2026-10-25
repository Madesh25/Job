"""Scheduled /autopilot (PR 12; twice a day since Autopilot H6, 25 Oct): fetch, screen,
resumes and contacts without a command.

It is off until Notion Config `schedule.autopilot` is `true`. When on, the first check at or
after each start time runs /autopilot once for that time (your decision of 25 Oct: 07:00 and
19:30 India time, every day, weekends too, so everything is ready when you apply at 22:00).
A check more than the window (3 hours) after a start time does not start it any more, so a
bot that was off in the morning does not run the morning run in the afternoon.

- Long polling (the bot on your laptop): the loop checks every few minutes.
- Webhook (Cloud Run): Cloud Scheduler calls POST /tasks/autopilot every 30 minutes after
  each start time; the first call starts the run, later calls carry on with its batch.

The run is the same as sending /autopilot: approvals are capped by
`screening.autopilot_approvals` a day (both runs together), and mail mode decides between
drafts and sending (sending keeps the recipient's morning, /mailqueue).
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
TIME_KEY = "schedule.autopilot_time"  # Notion Config: optional start times, "07:00, 19:30"
LAST_KEY = "autopilot.scheduled"  # bot_state: {"date": <local iso date>, "done": ["07:00"]}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULT_TIME = "07:00"
DEFAULT_WINDOW = timedelta(hours=3)
DEFAULT_ZONE = "Asia/Kolkata"
HOME_ZONE = "Asia/Kolkata"  # the status line also shows the time in India
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Plan:
    on: bool
    starts: tuple[time, ...]
    window: timedelta  # a start time is due for this long after it
    zone: ZoneInfo
    days: tuple[int, ...]  # 0 = Monday

    @property
    def start(self) -> time:
        return self.starts[0]

    def days_text(self) -> str:
        names = [DAYS[d] for d in self.days]
        if len(names) == 7:
            return "every day"
        if list(self.days) == list(range(self.days[0], self.days[-1] + 1)) and len(names) > 2:
            return f"{names[0]} to {names[-1]}"
        return ", ".join(names)

    def times_text(self) -> str:
        return " and ".join(f"{t:%H:%M}" for t in self.starts)


def _time(value: object, default: str) -> time:
    match = TIME_RE.match(str(value or "").strip())
    if not match:
        match = TIME_RE.match(default)
        assert match is not None
    return time(int(match.group(1)), int(match.group(2)))


def _times(value: object) -> tuple[time, ...]:
    """"07:00, 19:30" or a yaml list: the valid times, sorted (none valid: empty)."""
    items = value if isinstance(value, list) else str(value or "").replace(";", ",").split(",")
    return tuple(sorted({_time(v, "") for v in items if TIME_RE.match(str(v).strip())}))


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
    starts = (_times(config.get(TIME_KEY)) or _times(base.get("times"))
              or _times(base.get("time")) or (_time(DEFAULT_TIME, DEFAULT_TIME),))
    try:
        window = timedelta(hours=float(base.get("window_hours")))
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW
    return Plan(on=on, starts=starts, window=max(window, timedelta(0)),
                zone=ZoneInfo(str(base.get("timezone") or DEFAULT_ZONE)),
                days=_days(base.get("days")))


def local_now(desk: Desk, p: Plan) -> datetime:
    """Now in the schedule's time zone. A naive clock is the machine's own local time."""
    now = desk.now()
    return (now if now.tzinfo is not None else now.astimezone()).astimezone(p.zone)


def _done(desk: Desk, p: Plan) -> list[str]:
    last = desk.state.get(LAST_KEY) or {}
    if last.get("date") != local_now(desk, p).date().isoformat():
        return []
    # before H6 the state held only the date: that was the (one) morning run
    return list(last.get("done") or [f"{p.start:%H:%M}"])


def started(desk: Desk, p: Plan, at: time) -> bool:
    return f"{at:%H:%M}" in _done(desk, p)


def due(desk: Desk) -> time | None:
    """The start time that is due now (schedule on, within its window, not run yet)."""
    p = plan(desk)
    if not p.on:
        return None
    now = local_now(desk, p)
    if now.weekday() not in p.days:
        return None
    for at in p.starts:
        begin = datetime.combine(now.date(), at, tzinfo=now.tzinfo)
        if begin <= now <= begin + p.window and not started(desk, p, at):
            return at
    return None


def mark_started(desk: Desk, at: time | None = None) -> None:
    """Before the run: a failed run is not started again for that time (send /autopilot)."""
    p = plan(desk)
    at = at or p.start
    desk.state.set(LAST_KEY, {"date": local_now(desk, p).date().isoformat(),
                              "done": [*(d for d in _done(desk, p) if d != f"{at:%H:%M}"),
                                       f"{at:%H:%M}"]})


def next_start(desk: Desk, p: Plan) -> datetime:
    now = local_now(desk, p)
    for ahead in range(8):
        day = (now + timedelta(days=ahead)).date()
        if day.weekday() not in p.days:
            continue
        for at in p.starts:
            begin = datetime.combine(day, at, tzinfo=p.zone)
            if ahead == 0 and (started(desk, p, at) or now > begin + p.window):
                continue
            return begin
    return datetime.combine(now.date(), p.start, tzinfo=p.zone)  # pragma: no cover


def status_text(desk: Desk) -> str:
    """/autopilot when: whether the schedule is on, and the next run."""
    p = plan(desk)
    zone = p.zone.key
    hours = p.window.total_seconds() / 3600
    window = (f"{p.days_text()} at {p.times_text()} {zone} (a bot that is off then starts "
              f"it up to {hours:g} hours later)")
    if not p.on:
        return ("Scheduled autopilot: off. To run /autopilot by itself, set "
                f"Notion Config {SWITCH_KEY} to true. It would run {window}.")
    at = next_start(desk, p)
    home = "" if zone == HOME_ZONE else f" ({at.astimezone(ZoneInfo(HOME_ZONE)):%H:%M} in India)"
    ran = [f"{t:%H:%M}" for t in p.starts if started(desk, p, t)]
    done = f" Today's {' and '.join(ran)} run started." if ran else ""
    return (f"Scheduled autopilot: on, {window}.{done} Next: {at:%a %d %b %H:%M} {zone}{home}. "
            f"Turn it off with Notion Config {SWITCH_KEY} = false.")
