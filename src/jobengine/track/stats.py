"""/stats numbers (spec section 6.2). Pure.

A job counts as applied when it has an Applied date in the window. Its current Status says how
far it got: replied means Replied or better (Screening, Interview, Offer), and each later stage
counts the jobs that reached at least that stage. Reply rate = replied / applied.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from jobengine.track.status import JOB_ORDER, REPLIED_OR_BETTER

SPLITS = ("Country", "Board", "Screen verdict", "Sponsorship")
CONTACTED = ("Contacted", "Followed up", "Replied", "Ghosted", "Bounced", "Do not contact")


def _at_least(status: str | None, stage: str) -> bool:
    return status in JOB_ORDER and JOB_ORDER.index(status) >= JOB_ORDER.index(stage)


@dataclass
class Funnel:
    applied: int = 0
    replied: int = 0
    screening: int = 0
    interview: int = 0
    offer: int = 0
    rejected: int = 0
    ghosted: int = 0

    def add(self, status: str | None) -> None:
        self.applied += 1
        self.replied += status in REPLIED_OR_BETTER
        self.screening += _at_least(status, "Screening")
        self.interview += _at_least(status, "Interview")
        self.offer += status == "Offer"
        self.rejected += status == "Rejected"
        self.ghosted += status == "Ghosted"

    @property
    def rate(self) -> float:
        return self.replied / self.applied if self.applied else 0.0

    def line(self) -> str:
        return (f"applied {self.applied}, replied {self.replied}, screening {self.screening}, "
                f"interview {self.interview}, offer {self.offer}, rejected {self.rejected}, "
                f"ghosted {self.ghosted}; reply rate {pct(self.rate)}")


@dataclass
class ContactRate:
    contacted: int = 0
    replied: int = 0

    @property
    def rate(self) -> float:
        return self.replied / self.contacted if self.contacted else 0.0


@dataclass
class Stats:
    label: str
    funnel: Funnel = field(default_factory=Funnel)
    splits: dict[str, dict[str, Funnel]] = field(default_factory=dict)
    contacts: dict[str, ContactRate] = field(default_factory=dict)

    def text(self) -> str:
        lines = [f"{self.label}: {self.funnel.line()}"]
        for name, groups in self.splits.items():
            parts = [f"{key} {f.replied}/{f.applied} ({pct(f.rate)})"
                     for key, f in sorted(groups.items(), key=lambda kv: (-kv[1].applied, kv[0]))]
            if parts:
                lines.append(f"  by {name}: " + ", ".join(parts))
        parts = [f"{kind} {c.replied}/{c.contacted} ({pct(c.rate)})"
                 for kind, c in sorted(self.contacts.items())]
        if parts:
            lines.append("  contacts replied by Type: " + ", ".join(parts))
        return "\n".join(lines)


def pct(rate: float) -> str:
    return f"{round(rate * 100)}%"


def _in_window(day: Any, start: date | None, end: date) -> bool:
    return isinstance(day, date) and (start is None or start <= day) and day <= end


def compute(jobs: Iterable[dict[str, Any]], contacts: Iterable[dict[str, Any]], today: date,
            days: int | None, label: str) -> Stats:
    """Stats for jobs applied (and contacts mailed) in the last `days` days; None: all time."""
    start = today - timedelta(days=days - 1) if days else None
    stats = Stats(label=label, splits={name: {} for name in SPLITS})
    for values in jobs:
        if not _in_window(values.get("Applied date"), start, today):
            continue
        status = values.get("Status")
        stats.funnel.add(status)
        for name in SPLITS:
            key = values.get(name) or "Unknown"
            stats.splits[name].setdefault(str(key), Funnel()).add(status)
    for values in contacts:
        if values.get("Status") not in CONTACTED:
            continue
        if not _in_window(values.get("Last contacted"), start, today):
            continue
        rate = stats.contacts.setdefault(values.get("Type") or "Unknown", ContactRate())
        rate.contacted += 1
        rate.replied += values.get("Status") == "Replied"
    return stats


def stats_text(jobs: list[dict[str, Any]], contacts: list[dict[str, Any]], today: date) -> str:
    return "\n\n".join([compute(jobs, contacts, today, 30, "Last 30 days").text(),
                        compute(jobs, contacts, today, None, "All time").text()])
