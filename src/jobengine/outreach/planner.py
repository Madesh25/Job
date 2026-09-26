"""Chooses outreach or apply-only for an approved job and remembers the week's usage in
bot_state["outreach.week"] = {"week": "2026-W39", "allowance": 5, "jobs": [...]}."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from jobengine.bot_state import BotState
from jobengine.config_store import ConfigStore
from jobengine.contacts.credits import PROVIDERS, monthly_reset, parse_counter
from jobengine.outreach.budget import (
    Decision,
    JobFacts,
    decide,
    month_capacity,
    parse_costs,
    week_key,
    weekly_allowance,
    weeks_left,
)
from jobengine.reference import Reference

STATE_KEY = "outreach.week"
COST_KEY = "outreach.cost_per_job"
ON_IND = "On IND register"


@dataclass
class Budget:
    week: str
    allowance: int
    jobs: list[str]
    capacity: int
    costs: dict[str, int]
    counters: dict[str, Any]

    @property
    def used(self) -> int:
        return len(self.jobs)


def counters(config: ConfigStore, today: date) -> dict[str, Any]:
    out = {}
    for provider in PROVIDERS:
        key = f"credits.{provider}"
        counter = parse_counter(config.get(key), config.updated(key))
        out[provider] = monthly_reset(counter, today) if counter else None
    return out


def load_budget(state: BotState, config: ConfigStore, today: date) -> Budget:
    """This week's budget; a new week starts with a fresh allowance from the credits left."""
    costs = parse_costs(config.get(COST_KEY))
    current = counters(config, today)
    capacity = month_capacity(current, costs)
    week = week_key(today)
    saved = state.get(STATE_KEY) or {}
    if saved.get("week") == week:
        return Budget(week, int(saved.get("allowance") or 0), list(saved.get("jobs") or []),
                      capacity, costs, current)
    budget = Budget(week, weekly_allowance(capacity, today), [], capacity, costs, current)
    state.set(STATE_KEY, {"week": week, "allowance": budget.allowance, "jobs": []})
    return budget


def record(state: BotState, budget: Budget, job_id: str) -> None:
    if job_id not in budget.jobs:
        budget.jobs.append(job_id)
    state.set(STATE_KEY, {"week": budget.week, "allowance": budget.allowance,
                          "jobs": budget.jobs})


def facts(values: dict[str, Any], reference: Reference) -> JobFacts:
    company = reference.company(values.get("Company"))
    return JobFacts(verdict=values.get("Screen verdict"), sponsorship=values.get("Sponsorship"),
                    tier=company.tier_number if company else None,
                    on_ind_register=ON_IND in (values.get("Visa flags") or []))


def plan(state: BotState, config: ConfigStore, reference: Reference, values: dict[str, Any],
         job_id: str, today: date) -> Decision:
    """Decide for one job and, for outreach, count it against this week's allowance. A job
    already counted this week keeps outreach (a retry never spends a second slot)."""
    budget = load_budget(state, config, today)
    if job_id in budget.jobs:
        return Decision(True, "-", budget.allowance - budget.used, budget.allowance,
                        "already counted this week")
    decision = decide(facts(values, reference), budget.allowance, budget.used, budget.capacity)
    if decision.outreach:
        record(state, budget, job_id)
    return decision


def force(state: BotState, config: ConfigStore, job_id: str, today: date) -> Budget:
    """Your tap or /contacts: outreach regardless of priority; still counted."""
    budget = load_budget(state, config, today)
    record(state, budget, job_id)
    return budget


def status_text(state: BotState, config: ConfigStore, today: date) -> str:
    budget = load_budget(state, config, today)
    credits = ", ".join(
        f"{p.capitalize()} {c.left} left" if (c := budget.counters.get(p)) else
        f"{p.capitalize()} not set" for p in PROVIDERS)
    costs = ", ".join(f"{p} {budget.costs[p]}" for p in PROVIDERS)
    left = max(min(budget.allowance - budget.used, budget.capacity), 0)
    return "\n".join([
        f"Outreach budget, week {budget.week}: {budget.used} of {budget.allowance} jobs used, "
        f"{left} left.",
        f"Credits this month: {credits}. About {budget.capacity} more jobs this month "
        f"({weeks_left(today)} week(s) left; cost per job: {costs}).",
        "Every approved job still gets its resume and apply link. Outreach (paid contact "
        "lookup and cold mails) goes to Apply high first; Apply normal only while slots are "
        "left beyond those kept for Apply high; Apply low and Needs review only when you tap "
        "Find contacts anyway.",
    ])
