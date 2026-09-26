"""The weekly outreach allowance and the job priority. Pure.

Allowance: the jobs this month's remaining provider credits can cover (credits left divided
by the cost of one job's lookup, per provider), spread over the weeks left in the month.
Priority: A (Apply high with sponsorship stated, a large target company or the IND
register), B (other Apply high), C (Apply normal), D (Apply low, Needs review, anything else).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

from jobengine.contacts.credits import PROVIDERS, Counter

DEFAULT_COST = {"apollo": 4, "hunter": 1, "snov": 4}
PRIORITIES = ("A", "B", "C", "D")
LARGE_TIERS = frozenset({1, 2})
COST_PART = re.compile(r"(apollo|hunter|snov)\s*=\s*(\d+)", re.IGNORECASE)


def parse_costs(value: str | None) -> dict[str, int]:
    """Config outreach.cost_per_job ("apollo=4, hunter=1, snov=4"); missing parts default."""
    costs = dict(DEFAULT_COST)
    for name, number in COST_PART.findall(value or ""):
        costs[name.lower()] = max(int(number), 1)
    return costs


def month_capacity(counters: dict[str, Counter | None], costs: dict[str, int]) -> int:
    """How many more jobs the credits left this month can look up."""
    return sum((c.left // costs[p]) for p in PROVIDERS if (c := counters.get(p)))


def weeks_left(today: date) -> int:
    """Weeks left in the month, counting this one."""
    first_next = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    return max(1, math.ceil((first_next - today).days / 7))


def weekly_allowance(capacity: int, today: date) -> int:
    return math.ceil(capacity / weeks_left(today)) if capacity > 0 else 0


def week_key(today: date) -> str:
    year, week, _ = today.isocalendar()
    return f"{year}-W{week:02d}"


@dataclass(frozen=True)
class JobFacts:
    verdict: str | None
    sponsorship: str | None = None
    tier: int | None = None
    on_ind_register: bool = False


def priority(job: JobFacts) -> str:
    if job.verdict == "Apply high":
        strong = (job.sponsorship == "Stated yes" or job.tier in LARGE_TIERS
                  or job.on_ind_register)
        return "A" if strong else "B"
    if job.verdict == "Apply normal":
        return "C"
    return "D"


def reserve(allowance: int) -> int:
    """Slots kept for Apply high jobs later in the week (C jobs cannot use them)."""
    return max(1, allowance // 3) if allowance else 0


@dataclass(frozen=True)
class Decision:
    outreach: bool
    priority: str
    left: int  # allowance left this week before this job
    allowance: int
    reason: str


def decide(job: JobFacts, allowance: int, used: int, capacity: int) -> Decision:
    """Outreach for A and B while the week's allowance lasts; C only above the reserve kept
    for Apply high; D never automatically. No credits left: nobody."""
    level = priority(job)
    left = max(min(allowance - used, capacity), 0)
    if left <= 0:
        return Decision(False, level, left, allowance,
                        "this week's outreach budget is used up" if capacity > 0
                        else "no provider credits left this month")
    if level in ("A", "B"):
        return Decision(True, level, left, allowance, f"priority {level}")
    if level == "C" and left > reserve(allowance):
        return Decision(True, level, left, allowance, "priority C, budget to spare")
    if level == "C":
        return Decision(False, level, left, allowance,
                        f"the last {left} slot(s) this week are kept for Apply high jobs")
    verdict = job.verdict or "no verdict"
    return Decision(False, level, left, allowance, f"priority {level} ({verdict})")
