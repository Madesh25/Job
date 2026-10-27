"""AI cost guard (Autopilot I6, 26 Oct): a monthly cap on the paid AI.

Notion Config `ai.monthly_cap` is the most the AI may cost in a month, in US dollars (the
same estimate as the "AI in <month>" line, track/costs.py). Empty or 0: no cap.

- At 80% of the cap the bot warns you once that month.
- At 100% it pauses the paid AI until the 1st of next month: every AI call says so instead
  of running (screening, resumes, mails, contact search, /update). Reading your replies goes
  on: it costs about a cent a day and a missed interview reply costs more.
- The pause message has a button "Allow AI for the rest of <month>"; raising the cap in
  Config works too.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from jobengine.track.costs import KEY as COST_KEY

CAP_KEY = "ai.monthly_cap"
ALERT_KEY = "ai.cap_alert"  # bot_state: {"month": "2026-10", "level": 80 or 100}
ALLOW_KEY = "ai.cap_allow"  # bot_state: {"month": "2026-10"}: you allowed AI past the cap
WARN_SHARE = 0.8
FREE_STAGES = ("reply",)  # never paused: reading replies


def cap(config: Any) -> float | None:
    raw = str(config.get(CAP_KEY) or "").strip().lstrip("$").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _month(day: date) -> str:
    return day.strftime("%Y-%m")


def spent(state: Any, today: date) -> float:
    value = (state.get(COST_KEY) if state is not None else None) or {}
    return float(value.get("total") or 0.0) if value.get("month") == _month(today) else 0.0


def allowed_past_cap(state: Any, today: date) -> bool:
    value = (state.get(ALLOW_KEY) if state is not None else None) or {}
    return value.get("month") == _month(today)


def allow(state: Any, today: date) -> None:
    state.set(ALLOW_KEY, {"month": _month(today)})


def paused(config: Any, state: Any, today: date) -> bool:
    limit = cap(config)
    return (limit is not None and spent(state, today) >= limit
            and not allowed_past_cap(state, today))


def blocked(config: Any, state: Any, today: date, stage: str) -> str | None:
    """Why this AI call may not run now; None when it may."""
    if stage in FREE_STAGES or not paused(config, state, today):
        return None
    return (f"AI paused: this month's AI cost (about ${spent(state, today):.2f}) reached Config "
            f"{CAP_KEY} (${cap(config):.2f}). It starts again on the 1st, or tap \"Allow AI\" "
            "on the cap message")


def guard(state: Any, today: Callable[[], date]) -> Callable[[str, Any], str | None]:
    """What the bot passes to jobengine.llm.set_cost_guard."""
    return lambda stage, config: blocked(config, state, today(), stage)


def level(config: Any, state: Any, today: date) -> int:
    """100 at or over the cap, 80 from 80% of it, else 0."""
    limit = cap(config)
    if limit is None:
        return 0
    used = spent(state, today)
    return 100 if used >= limit else 80 if used >= WARN_SHARE * limit else 0


def alert(config: Any, state: Any, today: date) -> tuple[str, bool] | None:
    """(text, with the Allow button) once per month per level; None when nothing is new."""
    now = level(config, state, today)
    if not now or (now == 100 and allowed_past_cap(state, today)):
        return None
    sent = state.get(ALERT_KEY) or {}
    if sent.get("month") == _month(today) and int(sent.get("level") or 0) >= now:
        return None
    state.set(ALERT_KEY, {"month": _month(today), "level": now})
    used, limit = spent(state, today), cap(config) or 0.0
    month = today.strftime("%B")
    if now == 80:
        return (f"💸 AI cost: about ${used:.2f} of your ${limit:.2f} cap for {month} "
                f"({used / limit:.0%}). At the cap the paid AI pauses until the 1st. Raise "
                f"Config {CAP_KEY} if you need more.", False)
    return (f"⛔ AI paused: about ${used:.2f} used, your cap for {month} is ${limit:.2f} "
            f"(Config {CAP_KEY}). Screening, resumes, mails, contact search and /update wait "
            "until the 1st; reading your replies goes on. Tap below to allow AI for the rest "
            f"of {month}, or raise the cap in Config.", True)


def health_line(config: Any, state: Any, today: date) -> tuple[str, str] | None:
    """For the morning check: ("blocker" or "soon", text); None when under 80%."""
    now = level(config, state, today)
    if now == 100 and not allowed_past_cap(state, today):
        return ("blocker", f"AI paused: the {today:%B} cap (Config {CAP_KEY}, "
                f"${cap(config):.2f}) is reached. Fix: raise the cap, or wait for the 1st.")
    if now:
        return ("soon", f"AI cost at {spent(state, today) / (cap(config) or 1):.0%} of the "
                f"{today:%B} cap (Config {CAP_KEY}).")
    return None
