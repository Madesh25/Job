"""The Claude API cost of the month (PR 18), for the weekly digest.

Every LLM call reports its estimated cost (jobengine.llm.record_cost, the same prices as the
"AI used" lines); the bot adds it to a running total for the month in Bot State
`llm.cost_month`. A new month starts from zero and keeps last month's total for comparison.
It is an estimate from the token counts: Anthropic's console has the exact bill.
"""

from __future__ import annotations

import calendar
import threading
from collections.abc import Callable
from datetime import date
from typing import Any

from jobengine.bot_state import BotState

KEY = "llm.cost_month"  # bot_state: {"month", "total", "calls", "stages", "last"}
# LLM stage -> what it is in the digest ("score" is screening and reading replies).
STAGE_NAMES = {"score": "screening and replies", "tailor": "resumes", "email": "mails",
               "strategy": "strategy research", "fetch": "job pages"}
_lock = threading.Lock()  # screening runs calls on several threads


def _month(day: date) -> str:
    return day.strftime("%Y-%m")


def record(state: BotState, today: date, stage: str, dollars: float) -> None:
    with _lock:
        value = state.get(KEY) or {}
        month = _month(today)
        if value.get("month") != month:
            last = ({"month": value["month"], "total": value.get("total", 0.0)}
                    if value.get("month") else value.get("last"))
            value = {"month": month, "total": 0.0, "calls": 0, "stages": {}, "last": last}
        value["total"] = round(float(value.get("total") or 0.0) + dollars, 6)
        value["calls"] = int(value.get("calls") or 0) + 1
        stages: dict[str, Any] = dict(value.get("stages") or {})
        stages[stage] = round(float(stages.get(stage) or 0.0) + dollars, 6)
        value["stages"] = stages
        state.set(KEY, value)


def sink(state: BotState, today: Callable[[], date]) -> Callable[[str, float], None]:
    """What the bot passes to jobengine.llm.set_cost_sink."""
    return lambda stage, dollars: record(state, today(), stage, dollars)


def month_line(state: BotState, today: date) -> str:
    value = state.get(KEY) or {}
    name = today.strftime("%B")
    if value.get("month") != _month(today) or not value.get("calls"):
        return f"Claude API in {name}: nothing used yet."
    total = float(value.get("total") or 0.0)
    days = calendar.monthrange(today.year, today.month)[1]
    pace = total / max(1, today.day) * days
    stages = sorted((value.get("stages") or {}).items(), key=lambda kv: -kv[1])
    split = ", ".join(f"{STAGE_NAMES.get(k, k)} ${v:.2f}" for k, v in stages if v >= 0.005)
    text = (f"Claude API in {name} so far: about ${total:.2f} in {value['calls']} calls"
            + (f" ({split})" if split else "")
            + f"; on pace for about ${pace:.2f} this month.")
    last = value.get("last") or {}
    if last.get("month"):
        text += f" Last month: about ${float(last.get('total') or 0):.2f}."
    return text + " (Estimate from token counts; the Anthropic console has the exact bill.)"
