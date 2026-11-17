"""The daily check by itself (5 Nov, the autopilot timetable): 07:30 and 21:30 India time.

Before, the daily check (/today: replies, Applied from thank-you mails, rejections, bounces,
follow-ups, ghosting) ran only on Cloud Run at 07:30, and on the laptop only when you sent
/today. Replies and rejections come in the recipients' office hours (13:30 to 21:30 India
time), so the 21:45 report showed them only the next day.

Now it runs by itself at each time in Notion Config `schedule.tracking_times` (default
"07:30, 21:30", in the report time zone `schedule.report_timezone`, India by default), once
each a day, up to WINDOW after it (a bot that was off does not run the morning check in the
afternoon):

- 07:30: last night's "thank you for applying" mails mark your jobs Applied, and the
  follow-ups due today are queued for the recipients' morning.
- 21:30: after their office closes and before the 21:45 report, so the report and the
  22:00 apply nudge see today's replies and rejections.

Long polling (the laptop) checks it every few minutes. On Cloud Run the scheduler job
`je-daily-prod` calls /tasks/daily at both times; the once-a-day messages of that task (the
strategy reminder, the LinkedIn export reminder, credits, the AI cap, sponsors, the weekly
audit) come with the first call of the day only. Notion Config `schedule.tracking` =
`false` turns the timed check off (/today still works).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from jobengine.screen import schedule

SWITCH_KEY = "schedule.tracking"  # Notion Config: "false" turns the timed check off
TIMES_KEY = "schedule.tracking_times"  # Notion Config: "07:30, 21:30"
DEFAULT_TIMES = ("07:30", "21:30")
LAST_KEY = "tracking.scheduled"  # bot_state: {"date": <local iso date>, "done": ["07:30"]}
EXTRAS_KEY = "tracking.extras"  # bot_state: {"date": <local iso date>}
WINDOW = timedelta(hours=3)


def enabled(config: Any) -> bool:
    return (config.get(SWITCH_KEY) or "true").strip().lower() not in ("false", "off", "no")


def times(config: Any) -> tuple[time, ...]:
    return schedule._times(config.get(TIMES_KEY)) or schedule._times(list(DEFAULT_TIMES))


def _done(state: Any, day: date) -> list[str]:
    last = state.get(LAST_KEY) or {}
    return list(last.get("done") or []) if last.get("date") == day.isoformat() else []


def due(config: Any, state: Any, local: datetime) -> time | None:
    """The check time that is due now (on, within its window, not run yet today)."""
    if not enabled(config):
        return None
    done = _done(state, local.date())
    for at in times(config):
        begin = datetime.combine(local.date(), at, tzinfo=local.tzinfo)
        if begin <= local <= begin + WINDOW and f"{at:%H:%M}" not in done:
            return at
    return None


def mark(state: Any, day: date, at: time) -> None:
    state.set(LAST_KEY, {"date": day.isoformat(), "done": [*_done(state, day), f"{at:%H:%M}"]})


def extras_due(state: Any, day: date) -> bool:
    """The once-a-day messages of the Cloud Run task: true at the first call of the day."""
    if (state.get(EXTRAS_KEY) or {}).get("date") == day.isoformat():
        return False
    state.set(EXTRAS_KEY, {"date": day.isoformat()})
    return True


def text(config: Any) -> str:
    if not enabled(config):
        return "Daily check: off (Config schedule.tracking); /today runs it."
    return ("Daily check: by itself at " + " and ".join(f"{t:%H:%M}" for t in times(config))
            + " (replies, applications, rejections, follow-ups).")
