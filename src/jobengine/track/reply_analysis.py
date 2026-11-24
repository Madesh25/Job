"""Reply analysis (S9, 23 Nov, your ask): every reply sorted, and each week what got replies.

Every reply the daily check reads is already classified (track/classify.py); each one is now
also counted per day (day ledger `reply.<group>`) in four groups: interview (a screening call,
an interview or an offer), rejection (a no, or "do not contact"), automatic (a confirmation
or an out-of-office) and question (a positive or neutral answer, a question, anything else).

The Sunday weekly digest gets "📬 Replies": this week's replies by group, then, from every job
you applied to, which resume headline (your approved title the job's role names, the same
rule as the resume header) and which skills the job asked for (its Tech stack) got answers, so
the resume can follow what works. Pure, but for `record` (the day ledger).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta
from typing import Any

from jobengine.track.stats import pct
from jobengine.track.status import APPLIED_OR_LATER, REPLIED_OR_BETTER

GROUPS = {
    "interview": ("screening", "interview", "offer"),
    "rejection": ("rejection", "opt_out"),
    "automatic": ("ack", "auto_reply"),
    "question": ("positive", "neutral", "other"),
}
ORDER = tuple(GROUPS)
MIN_APPLIED = 2  # a headline or skill needs this many applications to be named
TOP_SKILLS = 6
DAYS = 7
DEFAULT_HEADLINE = "your main title"


def group_of(kind: str) -> str:
    return next((g for g, kinds in GROUPS.items() if kind in kinds), "question")


def record(kind: str) -> None:
    """Count one classified reply in today's day ledger."""
    from jobengine.track import day_ledger

    day_ledger.count(f"reply.{group_of(kind)}")


def week_counts(day: Callable[[date], Mapping[str, Any]], today: date) -> dict[str, int]:
    """The replies of the last DAYS days by group; `day` gives a day ledger entry."""
    out = dict.fromkeys(ORDER, 0)
    for n in range(DAYS):
        entry = day(today - timedelta(days=n)) or {}
        for group in ORDER:
            out[group] += int(float(entry.get(f"reply.{group}") or 0))
    return out


def _applied(values: Mapping[str, Any]) -> bool:
    return bool(values.get("Applied date")) or values.get("Status") in APPLIED_OR_LATER


def _skills(values: Mapping[str, Any]) -> list[str]:
    stack = values.get("Tech stack") or []
    if isinstance(stack, str):
        stack = [s.strip() for s in stack.split(",")]
    return list(dict.fromkeys(str(s) for s in stack if s))


class Tally:
    def __init__(self) -> None:
        self.applied = 0
        self.answered = 0  # replied, screening, interview or offer
        self.rejected = 0

    def add(self, status: str | None) -> None:
        self.applied += 1
        self.answered += status in REPLIED_OR_BETTER
        self.rejected += status == "Rejected"

    def line(self, name: str) -> str:
        rate = pct(self.answered / self.applied) if self.applied else "-"
        return (f"- {name}: {self.answered} of {self.applied} answered ({rate}), "
                f"{self.rejected} rejected")


def tallies(jobs: Iterable[Mapping[str, Any]],
            headline: Callable[[str | None], str | None]) -> tuple[dict[str, Tally],
                                                                   dict[str, Tally]]:
    """(by headline, by skill) over every job you applied to."""
    heads: dict[str, Tally] = defaultdict(Tally)
    skills: dict[str, Tally] = defaultdict(Tally)
    for values in jobs:
        if not _applied(values):
            continue
        status = values.get("Status")
        heads[headline(values.get("Role")) or DEFAULT_HEADLINE].add(status)
        for skill in _skills(values):
            skills[skill].add(status)
    return dict(heads), dict(skills)


def _ranked(rows: dict[str, Tally]) -> list[tuple[str, Tally]]:
    return sorted(rows.items(), key=lambda kv: (-kv[1].answered / kv[1].applied,
                                                -kv[1].applied, kv[0]))


def text(counts: Mapping[str, int], jobs: Iterable[Mapping[str, Any]],
         headline: Callable[[str | None], str | None]) -> str:
    """The "📬 Replies" block of the weekly digest."""
    week = ", ".join(f"{counts.get(g, 0)} {g}" for g in ORDER)
    lines = [f"📬 Replies this week: {week}."]
    heads, skills = tallies(jobs, headline)
    if not heads:
        lines.append("What got replies: nothing applied yet.")
        return "\n".join(lines)
    lines.append("What got replies (every job you applied to):")
    lines.append("By resume headline:")
    lines += [t.line(name) for name, t in _ranked(heads)]
    fair = {k: t for k, t in skills.items() if t.applied >= MIN_APPLIED}
    if fair:
        lines.append(f"By skill the job asked for (at least {MIN_APPLIED} applications):")
        lines += [t.line(name) for name, t in _ranked(fair)[:TOP_SKILLS]]
    best = [(k, t) for k, t in _ranked(heads) if t.answered]
    if best:
        lines.append(f"Most answers: the headline \"{best[0][0]}\" "
                     f"({best[0][1].answered} of {best[0][1].applied}).")
    elif sum(t.applied for t in heads.values()) >= 10:
        lines.append("No answer yet after 10 or more applications: look at the headline and "
                     "the first lines of the resume (Phase 23 S8 ATS check).")
    return "\n".join(lines)
