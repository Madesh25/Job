"""Afternoon job check (flow feature 2, 9 Oct): apply while a posting is fresh.

Jobs posted after the morning run wait until the next day, and the first applicants get the
most replies. When Notion Config `schedule.check` is `true`, the bot runs /fetch and then
/screen by itself at each time in `schedule.check_times` (default 15:00, Europe/Warsaw, the
days of the autopilot schedule) and sends the summary with an Apply high card for each new
strong match, so you can apply the same afternoon. Nothing is approved, built or mailed by
itself. Off by default.

- Long polling: the loop checks every few minutes; a time is due for CHECK_WINDOW after it,
  so a bot that was off then does not run it in the evening.
- Webhook (Cloud Run): Cloud Scheduler calls POST /tasks/check at the check time.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from jobengine.jobctl import CONTROL, Stopped
from jobengine.screen import schedule

if TYPE_CHECKING:
    from jobengine.screen.autopilot import Fetch, Progress
    from jobengine.screen.desk import Desk, Reply

SWITCH_KEY = "schedule.check"  # Notion Config: "true" turns it on
TIMES_KEY = "schedule.check_times"  # Notion Config: "15:00" or "12:30, 16:30"
LAST_KEY = "check.scheduled"  # bot_state: {"date": <local iso date>, "done": ["15:00"]}
DEFAULT_TIMES = "15:00"
CHECK_WINDOW = timedelta(hours=2)
NEXT_MARK = "\n\n\U0001F449 Next:"


def is_on(desk: Desk) -> bool:
    return (desk.deps.config().get(SWITCH_KEY) or "false").strip().lower() == "true"


def times(desk: Desk) -> list[time]:
    text = desk.deps.config().get(TIMES_KEY) or DEFAULT_TIMES
    picked = {schedule._time(part, "") for part in str(text).replace(";", ",").split(",")
              if schedule.TIME_RE.match(part.strip())}
    return sorted(picked) or [schedule._time(DEFAULT_TIMES, DEFAULT_TIMES)]


def _done(desk: Desk, now: datetime) -> list[str]:
    last = desk.state.get(LAST_KEY) or {}
    return list(last.get("done") or []) if last.get("date") == now.date().isoformat() else []


def due(desk: Desk) -> time | None:
    """The check time that is due now, or None."""
    if not is_on(desk):
        return None
    p = schedule.plan(desk)
    now = schedule.local_now(desk, p)
    if now.weekday() not in p.days:
        return None
    done = _done(desk, now)
    for at in times(desk):
        start = datetime.combine(now.date(), at, tzinfo=now.tzinfo)
        if start <= now <= start + CHECK_WINDOW and f"{at:%H:%M}" not in done:
            return at
    return None


def mark_done(desk: Desk, at: time) -> None:
    """Before the run: a failed check is not repeated that afternoon."""
    p = schedule.plan(desk)
    now = schedule.local_now(desk, p)
    desk.state.set(LAST_KEY, {"date": now.date().isoformat(),
                              "done": [*_done(desk, now), f"{at:%H:%M}"]})


def scheduled(desk: Desk, fetch: Fetch | None, progress: Progress) -> list[Reply] | None:
    """Run the check when it is due and nothing else runs. None when there is nothing to say."""
    from jobengine.screen.desk import Reply

    if desk.repo is None or fetch is None or CONTROL.running is not None:
        return None  # a /fetch, /screen or /autopilot is running: try at the next tick
    at = due(desk)
    if at is None:
        return None
    mark_done(desk, at)
    zone = schedule.plan(desk).zone.key
    try:
        with CONTROL.job("scheduled check"):
            summary = fetch(progress).split(NEXT_MARK)[0]
            screened = desk.screen_replies("", progress)
    except Stopped:
        return [Reply(f"Afternoon check ({at:%H:%M} {zone}) stopped by /end.")]
    head = Reply(f"Afternoon check ({at:%H:%M} {zone}): new jobs since this morning.\n\n"
                 f"{summary}")
    return [head, *screened]


def status_text(desk: Desk) -> str:
    """The line /autopilot when adds about the afternoon check."""
    when = ", ".join(f"{t:%H:%M}" for t in times(desk))
    zone = schedule.plan(desk).zone.key
    if not is_on(desk):
        return (f"Afternoon check: off. To run /fetch and /screen by themselves at {when} "
                f"{zone} (Apply high cards come at once), set Notion Config {SWITCH_KEY} to "
                f"true; {TIMES_KEY} changes the times.")
    return (f"Afternoon check: on, /fetch and /screen at {when} {zone} on the autopilot days. "
            f"Turn it off with Notion Config {SWITCH_KEY} = false.")
