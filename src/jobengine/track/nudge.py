"""Apply nudge (Autopilot H7, 25 Oct): gentle reminders until tonight's jobs are applied.

Your decision of 25 Oct (India time): at 22:00 the bot says how many jobs are ready (Status
"Resume built": resume done, not applied yet) and asks how your energy is: Low (1 easy job),
OK (3) or High (all). Then one job at a time with everything ready; "I applied" brings the
next. Until the goal is reached or you tap "Done for today", a reminder comes every 30
minutes, at most 5 an evening (until about 00:30). "Snooze 1 hour" skips two reminders.
A streak counts the evenings with at least one application, and the lines are always kind:
no shaming, a missed evening only starts a new streak. At the weekend the first message
offers to catch up on all that is left this week.

Notion Config: `schedule.nudge` (`false` turns it off), `schedule.nudge_time` (default 22:00),
`apply.minutes_per_job` (default 10, for the time estimate). India time
(`schedule.report_timezone`, like the daily report).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from jobengine.screen import schedule

SWITCH_KEY = "schedule.nudge"
TIME_KEY = "schedule.nudge_time"
MINUTES_KEY = "apply.minutes_per_job"
DEFAULT_TIME = "22:00"
DEFAULT_MINUTES = 10
EVENING_KEY = "nudge.evening"  # bot_state: tonight's nudge (see Evening)
STREAK_KEY = "nudge.streak"  # bot_state: {"last": "<evening date>", "days": n}
EVERY = timedelta(minutes=30)
MAX_REMINDERS = 5
SNOOZE = timedelta(hours=1)
WINDOW = EVERY * MAX_REMINDERS  # the last reminder comes about 2.5 hours after the start
GOALS = {"low": 1, "ok": 3}  # "high": all that are ready
ENERGY = (("🪫 Low (1 easy one)", "low"), ("🙂 OK (3)", "ok"), ("⚡ High (all)", "high"))


def is_on(config: Any) -> bool:
    return (config.get(SWITCH_KEY) or "true").strip().lower() != "false"


def minutes_per_job(config: Any) -> int:
    try:
        return max(1, int(str(config.get(MINUTES_KEY) or DEFAULT_MINUTES).strip()))
    except ValueError:
        return DEFAULT_MINUTES


def evening_of(here: datetime) -> date:
    """The evening a time belongs to: 00:30 on Friday is still Thursday's evening."""
    return (here - timedelta(hours=12)).date()


def start_of(config: Any, evening: date, here: datetime) -> datetime:
    at = schedule._time(config.get(TIME_KEY), DEFAULT_TIME)
    return datetime.combine(evening, at, tzinfo=here.tzinfo)


def get(state: Any, evening: date) -> dict[str, Any]:
    """Tonight's nudge: {"evening", "sent", "last", "snooze", "goal", "applied", "done"}."""
    value = (state.get(EVENING_KEY) if state is not None else None) or {}
    if value.get("evening") != evening.isoformat():
        return {"evening": evening.isoformat(), "sent": 0, "applied": 0}
    return dict(value)


def save(state: Any, value: dict[str, Any]) -> None:
    if state is not None:
        state.set(EVENING_KEY, value)


def active(state: Any, here: datetime) -> bool:
    """A nudge goes tonight and is not finished (I applied then brings the next ready job)."""
    value = get(state, evening_of(here))
    return bool(value.get("sent")) and not value.get("done")


def goal(value: dict[str, Any], ready: int) -> int | None:
    energy = value.get("goal")
    if energy is None:
        return None
    return GOALS.get(energy, ready + int(value.get("applied") or 0))


def due(config: Any, state: Any, here: datetime, ready: int) -> bool:
    """A first message or a reminder is due now."""
    if not is_on(config) or ready <= 0:
        return False
    evening = evening_of(here)
    start = start_of(config, evening, here)
    if not start <= here <= start + WINDOW:
        return False
    value = get(state, evening)
    if value.get("done") or int(value.get("sent") or 0) >= MAX_REMINDERS:
        return False
    target = goal(value, ready)
    if target is not None and int(value.get("applied") or 0) >= target:
        return False
    for key, wait in (("last", EVERY), ("snooze", timedelta(0))):
        if value.get(key):
            try:
                if here < datetime.fromisoformat(value[key]) + wait:
                    return False
            except ValueError:
                pass
    return True


def first_text(ready: int, minutes: int, weekend: bool, streak: int) -> str:
    lines = [f"🎯 {ready} job(s) ready to apply, about {ready * minutes} minutes in all "
             f"(about {minutes} each). How is your energy tonight?"]
    if weekend:
        lines.append("Weekend: High catches up on everything left this week.")
    if streak:
        lines.append(f"🔥 {streak}-evening streak so far.")
    return "\n".join(lines)


def reminder_text(ready: int, applied: int, target: int | None, minutes: int) -> str:
    if applied and target:
        return (f"⏰ {applied} of {target} done, nice. {ready} still ready: the next one takes "
                f"about {minutes} minutes.")
    if applied:
        return f"⏰ {applied} done tonight, nice. {ready} still ready. One more?"
    return f"⏰ {ready} job(s) still ready. Just one takes about {minutes} minutes."


def card_text(values: dict[str, Any], number: int, target: int | None, ref: str) -> str:
    place = ", ".join(p for p in (values.get("City"), values.get("Country")) if p)
    of = f" of {target}" if target else ""
    return (f"Job {number}{of}: {values.get('Company') or '?'}, {values.get('Role') or '?'}"
            f"{f' ({place})' if place else ''}\nApply: {values.get('URL') or '(no link)'}\n"
            f"Resume, Apply pack and autofill code: /applypack {ref}\n"
            f"Let the bot fill the form first: /dryrun {ref}")


def finish_text(applied: int, streak: int) -> str:
    if not applied:
        return ("Done for today. Rest well: tomorrow's jobs will be ready at 22:00. A new "
                "evening is a fresh start.")
    days = f" 🔥 {streak}-evening streak." if streak > 1 else ""
    return f"🎉 Well done: {applied} application(s) tonight.{days} See you tomorrow."


def record_streak(state: Any, evening: date) -> int:
    """An evening with at least one application: the streak grows (or starts again)."""
    value = (state.get(STREAK_KEY) if state is not None else None) or {}
    last = value.get("last")
    days = int(value.get("days") or 0)
    if last == evening.isoformat():
        return days
    days = days + 1 if last == (evening - timedelta(days=1)).isoformat() else 1
    if state is not None:
        state.set(STREAK_KEY, {"last": evening.isoformat(), "days": days})
    return days


def streak(state: Any, evening: date) -> int:
    """The streak to show tonight (0 once an evening was missed)."""
    value = (state.get(STREAK_KEY) if state is not None else None) or {}
    last = value.get("last")
    if last in (evening.isoformat(), (evening - timedelta(days=1)).isoformat()):
        return int(value.get("days") or 0)
    return 0


def energy_buttons() -> list[tuple[str, str]]:
    return [(label, f"en:{code}") for label, code in ENERGY]


def later_buttons() -> list[tuple[str, str]]:
    return [("😴 Snooze 1 hour", "sz:1"), ("✅ Done for today", "dd:1")]


def ready_ids(rows: Sequence[tuple[str, dict[str, Any]]]) -> list[str]:
    """Oldest first: the jobs built earliest have waited longest."""
    def key(row: tuple[str, dict[str, Any]]) -> str:
        return str(row[1].get("Last activity date") or row[1].get("First seen") or "")
    return [page_id for page_id, _ in sorted(rows, key=key)]
