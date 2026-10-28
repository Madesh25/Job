"""Learning from fast rejections (28 Oct, your OK), for the weekly audit.

A rejection within FAST_DAYS of applying is usually an automatic filter (an ATS knock-out
question or a recruiter's quick rule), not a person who read the resume. Applying the same
way again gets the same answer, so the audit compares the fast rejections of the last
WINDOW_DAYS with the other applications of that time and names what the fast ones share
more often:

- they asked more years than you have (Years required of YEARS_HIGH or more);
- the screening found 2 or more skills you do not have (Gaps);
- they need Polish or Dutch;
- they say no visa sponsorship, the company is not on the IND register, or the job came
  through an Irish agency;
- a Senior title;
- screening rated them Apply low or Needs review;
- one country or one job board.

A cause is named when at least MIN_FAST fast rejections exist, at least half of them share it
and it is at least GAP points more common than in the other applications. Counted from your
Job Opportunities rows, no AI. The fix is yours (the resume, the screening rules or where you
apply); Opus helps with the audit page.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from jobengine.screen import visa
from jobengine.screen.models import split_gaps
from jobengine.sweep.normalize import years_low

FAST_DAYS = 3
WINDOW_DAYS = 56  # 8 weeks of applications
MIN_FAST = 3
SHARE = 0.5
GAP = 0.2
YEARS_HIGH = 4  # you have 3 years in DevOps (apply_pack.experience)
SHOWN = 8


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _flags(v: Mapping[str, Any]) -> list[str]:
    return [str(f) for f in v.get("Visa flags") or []]


# (name, the advice when it is the cause, test). Each test reads one job's Notion values.
Check = tuple[str, str, Callable[[Mapping[str, Any]], bool]]
CHECKS: tuple[Check, ...] = (
    (f"asked {YEARS_HIGH}+ years",
     "skip jobs asking 4+ years, or show your years more clearly at the top of the resume",
     lambda v: (years_low(v.get("Years required")) or 0) >= YEARS_HIGH),
    ("2+ missing skills",
     "the resume lacks their must-have tools: add the ones you really have (Add keywords), "
     "skip jobs with 2+ real gaps",
     lambda v: len(split_gaps(v.get("Gaps"))) >= 2),
    ("Polish or Dutch needed",
     "skip jobs that need Polish or Dutch",
     lambda v: str(v.get("Language required") or "").startswith(("Polish", "Dutch"))),
    ("no sponsorship sign",
     "favour jobs that state sponsorship, IND sponsors and Irish permit holders",
     lambda v: v.get("Sponsorship") == "Stated no"
     or bool({visa.NOT_ON_IND, visa.AGENCY_IE} & set(_flags(v)))),
    ("Senior title", "Senior titles reject fast: apply to mid-level titles first",
     lambda v: v.get("Seniority") == "Senior"),
    ("weak screening verdict", "do not apply to Apply low or Needs review jobs",
     lambda v: v.get("Screen verdict") in ("Apply low", "Needs review")),
)
GROUPS = ("Country", "Board")


@dataclass
class Cause:
    name: str
    fast: int  # fast rejections with it
    fast_total: int
    other_share: float
    advice: str

    def line(self) -> str:
        return (f"🔴 {self.fast} of {self.fast_total} fast rejections {self.name} "
                f"({self.fast / self.fast_total:.0%}; other applications "
                f"{self.other_share:.0%}). Try: {self.advice}.")


@dataclass
class Review:
    fast: list[Mapping[str, Any]] = field(default_factory=list)
    others: int = 0
    causes: list[Cause] = field(default_factory=list)


def split(jobs: Iterable[Mapping[str, Any]], today: date
          ) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """(fast rejections, the other applications) applied in the last WINDOW_DAYS."""
    fast, others = [], []
    start = today - timedelta(days=WINDOW_DAYS)
    for v in jobs:
        applied = _day(v.get("Applied date"))
        if applied is None or not start <= applied <= today:
            continue
        closed = _day(v.get("Last activity date"))
        if v.get("Status") == "Rejected" and closed is not None \
                and (closed - applied).days <= FAST_DAYS:
            fast.append(v)
        else:
            others.append(v)
    return fast, others


def review(jobs: Iterable[Mapping[str, Any]], today: date) -> Review:
    fast, others = split(jobs, today)
    out = Review(fast, len(others))
    if len(fast) < MIN_FAST:
        return out
    tests: list[Check] = list(CHECKS)
    for group in GROUPS:
        for value in sorted({str(v.get(group) or "") for v in fast} - {""}):
            tests.append((f"were {group.lower()} {value}",
                          f"look at the {value} applications: a different resume angle, or "
                          "apply there less",
                          lambda v, g=group, x=value: str(v.get(g) or "") == x))
    for name, advice, test in tests:
        hit = sum(1 for v in fast if test(v))
        share = hit / len(fast)
        other = sum(1 for v in others if test(v)) / len(others) if others else 0.0
        if share >= SHARE and share - other >= GAP:
            out.causes.append(Cause(name, hit, len(fast), other, advice))
    out.causes.sort(key=lambda c: -(c.fast / c.fast_total - c.other_share))
    return out


def audit_lines(jobs: Iterable[Mapping[str, Any]], today: date) -> list[str]:
    """The weekly audit section: the likely causes, then the fast rejections themselves."""
    result = review(jobs, today)
    weeks = WINDOW_DAYS // 7
    if len(result.fast) < MIN_FAST:
        return [f"- {len(result.fast)} rejection(s) within {FAST_DAYS} days in the last {weeks} "
                f"weeks: too few to see a pattern (needs {MIN_FAST})."]
    lines = [f"- {len(result.fast)} rejections within {FAST_DAYS} days of applying in the "
             f"last {weeks} weeks (other applications: {result.others}). Fast rejections are "
             "usually automatic filters."]
    lines += [c.line() for c in result.causes] or [
        "- No single cause stands out: compare the jobs below with Opus."]
    for v in result.fast[:SHOWN]:
        applied, closed = _day(v.get("Applied date")), _day(v.get("Last activity date"))
        days = (closed - applied).days if applied and closed else "?"
        facts = [f for f in (str(v.get("Years required") or ""), str(v.get("Country") or ""),
                             str(v.get("Screen verdict") or "")) if f]
        gaps = ", ".join(split_gaps(v.get("Gaps"))[:3])
        lines.append(f"   {v.get('Company') or '?'}, {v.get('Role') or '?'}: rejected after "
                     f"{days} day(s); {'; '.join(facts)}" + (f"; gaps: {gaps}" if gaps else ""))
    if len(result.fast) > SHOWN:
        lines.append(f"   ... and {len(result.fast) - SHOWN} more")
    return lines
