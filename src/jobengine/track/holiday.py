"""Holiday mode (Autopilot I8, 27 Oct): /pause 5d, /pause 2026-11-05, /resume.

While it is on, the autopilot still fetches and screens (so nothing is missed), but approves
nothing, builds no resume and sends no mail: the mail queue waits, follow-up drafts wait,
auto-apply and the evening apply nudge stop. Your own commands still work. It ends by itself
on the day you are back (that day's first autopilot summary starts with a welcome back), or
at once with /resume. Until then the morning check says when it ends.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

KEY = "holiday"  # bot_state: {"back": <iso date you are back>, "since": <iso date>}
MAX_DAYS = 60
USAGE = ("Send /pause 5d (5 days) or /pause 2026-11-05 (the day you are back). The autopilot "
         "keeps finding and screening jobs, but approves nothing and sends no mail. /resume "
         "ends it early.")
STOPS = ("no job approved, no resume built, no mail sent; follow-ups, auto-apply and the "
         "evening nudge wait")


def back_on(state: Any) -> date | None:
    value = (state.get(KEY) if state is not None else None) or {}
    try:
        return date.fromisoformat(str(value.get("back") or ""))
    except ValueError:
        return None


def active(state: Any, today: date) -> date | None:
    """The day you are back while holiday mode is on, else None."""
    back = back_on(state)
    return back if back is not None and today < back else None


def parse(args: str, today: date) -> date | str:
    """The day you are back, or why the words are not understood."""
    word = args.strip().lower()
    match = re.fullmatch(r"(\d{1,3})\s*(d|day|days)?", word)
    if match:
        days = int(match.group(1))
        back = today + timedelta(days=days)
    else:
        try:
            back = date.fromisoformat(word)
        except ValueError:
            return USAGE
    if back <= today:
        return "That day has passed. " + USAGE
    if (back - today).days > MAX_DAYS:
        return f"At most {MAX_DAYS} days. " + USAGE
    return back


def pause(state: Any, args: str, today: date) -> str:
    if not args.strip():
        back = active(state, today)
        return (f"🏖 Holiday mode is on until {back:%a %d %b}: {STOPS}. /resume ends it."
                if back else USAGE)
    back = parse(args, today)
    if isinstance(back, str):
        return back
    state.set(KEY, {"back": back.isoformat(), "since": today.isoformat()})
    return (f"🏖 Holiday mode on until {back:%a %d %b} (you are back that day). The autopilot "
            f"keeps finding and screening jobs; {STOPS}. /resume ends it early.")


def resume(state: Any, today: date) -> str:
    if active(state, today) is None:
        state.delete(KEY)
        return "Holiday mode is not on. /pause 5d starts it."
    state.delete(KEY)
    return ("✅ Holiday mode ended: the autopilot approves jobs, builds resumes and sends "
            "mails again. /pending has the jobs screened while you were away.")


def ended(state: Any, today: date) -> str | None:
    """Once, on the day you are back: the message, and holiday mode is cleared."""
    back = back_on(state)
    if back is None or today < back:
        return None
    state.delete(KEY)
    return ("✅ Welcome back: holiday mode ended today. The autopilot approves jobs, builds "
            "resumes and sends mails again. /pending has the jobs screened while you were "
            "away.")
