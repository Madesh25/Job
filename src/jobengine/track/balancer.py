"""Credit balancer (3 Nov, your idea): use the Google credit first while there is room.

Plan C stays the base: DeepSeek does the work, Gemini the Google searches, Claude is the
backup. The Google Cloud credit (₹959.89, about $10 a month, each grant usable for a year)
pays for Gemini. While this month's Gemini budget (Config `ai.gemini_cap`) has room left
after a reserve for the searches, DeepSeek's most expensive jobs run on Gemini instead:
resume builds, then cover letters, then form answers, then screening. A plain code rule, no
AI and no tokens.

- At most once an hour (the first AI call of the hour, and the morning check) the bot
  works out:
  budget left = ai.gemini_cap - what Gemini cost this month;
  reserve = what the searches will still cost this month at this month's pace (at least 20%
  of the cap); room = budget left - reserve.
  Each job is moved while its cost for the rest of the month, on Gemini (about 4 times the
  DeepSeek price), still fits in the room.
- Empty ai.gemini_cap, or Config `ai.balance` = off: no moves (plan C as before).
- A Gemini call that fails goes back to DeepSeek; at the cap Gemini stops and DeepSeek
  takes everything (llm.ModelRouter). Claude stays the backup.
"""

from __future__ import annotations

import calendar
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from jobengine.track import cost_guard
from jobengine.track.costs import KEY as COST_KEY

log = logging.getLogger(__name__)

SWITCH_KEY = "ai.balance"  # Config: "off" keeps plan C
MODEL_KEY = "ai.balance_model"  # Config: the Gemini model for the moved jobs
DEFAULT_MODEL = "gemini-3.5-flash-lite"
# Most expensive first (your order). "tailor" is the resume stage's old name.
STAGES = ("resume", "cover", "apply", "score")
STAGE_WORDS = {"resume": "resumes", "cover": "cover letters", "apply": "form answers",
               "score": "screening"}
SEARCH_STAGES = ("fetch", "people", "strategy")
GEMINI_FACTOR = 4.0  # gemini-3.5-flash-lite costs about 4 times deepseek-v4-flash a call
MIN_RESERVE_SHARE = 0.2  # of the cap, kept for the searches at the least


@dataclass
class Plan:
    stages: list[str] = field(default_factory=list)  # moved to Gemini today
    model: str = DEFAULT_MODEL
    cap: float | None = None
    used: float = 0.0
    reserve: float = 0.0
    room: float = 0.0
    why: str = ""


def _off(config: Any) -> bool:
    return str(config.get(SWITCH_KEY) or "").strip().lower() in ("off", "no", "false", "0")


def _model(config: Any) -> str:
    raw = str(config.get(MODEL_KEY) or "").split()
    model = raw[0] if raw else DEFAULT_MODEL
    return model if model.startswith("gemini-") else DEFAULT_MODEL


def _month_spent(state: Any, today: date) -> tuple[float, dict[str, float]]:
    """(Gemini's cost this month, each job's cost this month) from one Bot State read."""
    value = (state.get(COST_KEY) if state is not None else None) or {}
    if value.get("month") != today.strftime("%Y-%m"):
        return 0.0, {}
    gemini = float((value.get("providers") or {}).get("gemini") or 0.0)
    out: dict[str, float] = {}
    for stage, dollars in (value.get("stages") or {}).items():
        name = "resume" if stage == "tailor" else str(stage)
        out[name] = out.get(name, 0.0) + float(dollars or 0.0)
    return gemini, out


def plan(config: Any, state: Any, today: date) -> Plan:
    """Which DeepSeek jobs run on Gemini today, and why."""
    model = _model(config)
    limit = cost_guard.cap(config, cost_guard.GEMINI_CAP_KEY)
    if _off(config):
        return Plan(model=model, cap=limit, why=f"Config {SWITCH_KEY} is off")
    if limit is None:
        return Plan(model=model, why=f"no Config {cost_guard.GEMINI_CAP_KEY}")
    used, spent = _month_spent(state, today)
    days = calendar.monthrange(today.year, today.month)[1]
    elapsed = max(1, today.day)
    left_days = days - today.day + 1  # today counts: the plan is made in the morning

    def rest_of_month(dollars: float) -> float:
        return dollars / elapsed * left_days

    reserve = max(MIN_RESERVE_SHARE * limit,
                  rest_of_month(sum(spent.get(s, 0.0) for s in SEARCH_STAGES)))
    room = limit - used - reserve
    out = Plan(model=model, cap=limit, used=used, reserve=reserve, room=room)
    if room <= 0:
        out.why = "the Google credit left is kept for the searches"
        return out
    for stage in STAGES:
        need = rest_of_month(spent.get(stage, 0.0)) * GEMINI_FACTOR
        if need > room:
            continue  # a cheaper job may still fit
        out.stages.append(stage)
        room -= need
    out.room = room
    out.why = "the Google credit has room" if out.stages else "no job fits in the room left"
    return out


def line(config: Any, state: Any, today: date) -> str:
    """For the morning check: what runs where and why."""
    p = plan(config, state, today)
    if p.cap is None or _off(config):
        return f"Credit balancer: off ({p.why}); plan C (Gemini only for the searches)."
    money = (f"Gemini ${p.used:.2f} of ${p.cap:.2f} this month, ${p.reserve:.2f} kept for "
             "the searches")
    if p.stages:
        jobs = ", ".join(STAGE_WORDS[s] for s in p.stages)
        return f"Credit balancer: {jobs} run on {p.model} today ({money})."
    return f"Credit balancer: plan C today, {p.why} ({money})."


REPLAN_SECONDS = 3600


class Balancer:
    """What the bot passes to jobengine.llm.set_balancer: the plan is worked out at most once
    an hour and kept in memory, so an AI call adds no Notion read."""

    def __init__(self, state: Any, today: Callable[[], date],
                 clock: Callable[[], float] = time.monotonic):
        self.state = state
        self.today = today
        self.clock = clock
        self._lock = threading.Lock()
        self._day: date | None = None
        self._at = 0.0
        self._plan = Plan()

    def __call__(self, stage: str, config: Any) -> str | None:
        name = "resume" if stage == "tailor" else stage
        if name not in STAGES:
            return None
        day, now = self.today(), self.clock()
        with self._lock:
            if self._day != day or now - self._at >= REPLAN_SECONDS:
                try:
                    self._plan = plan(config, self.state, day)
                except Exception:  # the balancer saves money: never fail a call over it
                    log.exception("could not plan the credit balancer: plan C")
                    self._plan = Plan()
                self._day, self._at = day, now
            p = self._plan
        return p.model if name in p.stages else None
