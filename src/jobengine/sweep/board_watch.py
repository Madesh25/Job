"""The weekly job board check (28 Oct, your request): `--check-boards` by itself once a week.

Every Sunday from START (India time), or the first day after when the bot was off, the bot
checks every Target Company's job board the way `python -m jobengine.sweep --check-boards`
does (sweep/boards.py: reads only, writes nothing to Notion). It keeps the result in Bot State
and compares it with last week's:

- a board that worked and now fails (HTTP 404, a moved site ...);
- a board that had jobs in Poland, the Netherlands or Ireland and now has none;
- a board whose jobs fell to half or less (from MIN_JOBS or more);
- a board that failed and works again.

You get a Telegram message only when something changed (the first run sends one short
summary). The weekly audit (track/audit.py) shows the same result for Opus.

On by default only on the prod bot (after go-live). Notion Config `schedule.board_check`
= `true` or `false` overrides that.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jobengine.sweep.boards import Check

SWITCH_KEY = "schedule.board_check"
KEY = "boards.weekly"  # bot_state: {"date", "rows": {company: [result, jobs, here]}, ...}
SUNDAY = 6
START = time(10, 0)  # after the link check (09:00), before the weekly audit (20:00)
MIN_JOBS = 10  # a fall to half counts from this many jobs
SHOWN = 15
OK = "OK"


def is_on(s: Any, config: Any) -> bool:
    value = (config.get(SWITCH_KEY) or "").strip().lower()
    if value in ("true", "false"):
        return value == "true"
    return getattr(s, "app_env", "") == "prod"


def due(s: Any, config: Any, state: Any, now: datetime) -> bool:
    """From Sunday START; a week after the last check when the bot missed a Sunday."""
    if not is_on(s, config) or now.time() < START:
        return False
    raw = ((state.get(KEY) if state is not None else None) or {}).get("date")
    try:
        last = date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        last = None
    if last is None:
        return now.weekday() == SUNDAY
    return (now.date() - last).days >= 7


@dataclass
class Week:
    rows: dict[str, list[Any]]  # company -> [result, jobs, here]
    changes: list[str] = field(default_factory=list)
    first: bool = False

    @property
    def ok(self) -> int:
        return sum(1 for r in self.rows.values() if r[0] == OK)

    @property
    def with_jobs_here(self) -> int:
        return sum(1 for r in self.rows.values() if r[0] == OK and (r[2] or 0) > 0)

    def failing(self) -> list[str]:
        return [f"{name} ({r[0]})" for name, r in self.rows.items()
                if r[0] != OK and _readable_kind(r[0])]


def _readable_kind(result: str) -> bool:
    """A result that is a problem to fix (not "own job site, covered by your alerts")."""
    return not result.startswith("own job site")


def rows_of(checks: Iterable[Check]) -> dict[str, list[Any]]:
    return {c.company: [c.result, c.jobs, c.here] for c in checks}


def compare(before: Mapping[str, list[Any]], now: Mapping[str, list[Any]]) -> list[str]:
    """What changed since last week, one line a board."""
    out = []
    for name, (result, jobs, here) in now.items():
        old = before.get(name)
        if not old:
            continue
        old_result, old_jobs, old_here = (list(old) + [None, None, None])[:3]
        if old_result == OK and result != OK and _readable_kind(result):
            out.append(f"🔴 {name}: the board stopped working ({result}); it had "
                       f"{old_jobs or 0} jobs. Check its Careers URL in Target Companies.")
        elif old_result != OK and result == OK:
            out.append(f"🟢 {name}: the board works again ({jobs or 0} jobs, {here or 0} in "
                       "PL/NL/IE).")
        elif result == OK and (old_here or 0) > 0 and not here:
            out.append(f"🟠 {name}: no jobs in PL/NL/IE now (last week {old_here}).")
        elif result == OK and (old_jobs or 0) >= MIN_JOBS and (jobs or 0) * 2 <= old_jobs:
            out.append(f"🟠 {name}: {jobs or 0} jobs, last week {old_jobs}: the board may have "
                       "moved or changed its search.")
    return out


def record(state: Any, today: date, checks: list[Check]) -> Week:
    """Compare with last week and keep this week's result in Bot State."""
    before = (state.get(KEY) if state is not None else None) or {}
    rows = rows_of(checks)
    week = Week(rows, compare(before.get("rows") or {}, rows), first=not before.get("rows"))
    if state is not None:
        state.set(KEY, {"date": today.isoformat(), "rows": rows, "changes": week.changes,
                        "ok": week.ok, "total": len(rows), "here": week.with_jobs_here})
    return week


def text(week: Week) -> str:
    """The Telegram message, or "" when nothing changed."""
    head = (f"🔎 Weekly job board check: {week.ok} of {len(week.rows)} boards readable, "
            f"{week.with_jobs_here} with jobs in PL/NL/IE.")
    if week.first:
        lines = [head + " (First run: from next week you hear only about changes.)"]
        failing = week.failing()
        if failing:
            lines.append(f"Not readable now ({len(failing)}): " + ", ".join(failing[:SHOWN])
                         + (f" and {len(failing) - SHOWN} more" if len(failing) > SHOWN
                            else ""))
        return "\n".join(lines)
    if not week.changes:
        return ""
    lines = [head, f"Changed since last week ({len(week.changes)}):", *week.changes[:SHOWN]]
    if len(week.changes) > SHOWN:
        lines.append(f"... and {len(week.changes) - SHOWN} more (in the weekly audit).")
    return "\n".join(lines)


def audit_lines(value: Mapping[str, Any] | None, monday: date, sunday: date) -> list[str]:
    """The weekly audit's lines about the board check of that week."""
    value = value or {}
    try:
        day = date.fromisoformat(str(value.get("date") or ""))
    except ValueError:
        day = None
    if day is None or not monday <= day <= sunday:
        return ["- No board check this week (it runs on Sundays on the prod bot; "
                "`python -m jobengine.sweep --check-boards` by hand)."]
    lines = [f"- {value.get('ok', 0)} of {value.get('total', 0)} boards readable, "
             f"{value.get('here', 0)} with jobs in PL/NL/IE (checked {day:%d %b})."]
    changes = list(value.get("changes") or [])
    lines += [f"- {c}" for c in changes[:SHOWN]] or ["- No board changed since last week."]
    return lines

