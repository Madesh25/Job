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


# ---------------------------------------------------------------- what gets replies (PR 9)

REPORT_SPLITS = (("Board", "board"), ("Country", "country"))
# Contacts Type -> the mail each type gets (mail/templates.py KIND).
MAIL_KIND = {"Peer engineer": "referral ask (engineers)", "Recruiter/TA": "cold mail (HR)",
             "Hiring": "cold mail (hiring managers)", "Other": "cold mail (mailboxes)"}
DEFAULT_MIN_SAMPLE = 3


def _group_line(key: str, f: Funnel) -> str:
    line = f"{key} {f.replied}/{f.applied} replied ({pct(f.rate)})"
    return f"{line}, {f.interview} interview{'s' if f.interview != 1 else ''}" \
        if f.interview else line


def _best_and_worst(groups: dict[str, Funnel], min_sample: int) -> str | None:
    fair = {k: f for k, f in groups.items() if f.applied >= min_sample and k != "Unknown"}
    if len(fair) < 2:
        return None
    ranked = sorted(fair.items(), key=lambda kv: (-kv[1].rate, -kv[1].interview, kv[0]))
    (best, fb), (worst, fw) = ranked[0], ranked[-1]
    if fb.rate == fw.rate:
        return None
    return f"most replies from {best} ({pct(fb.rate)}), fewest from {worst} ({pct(fw.rate)})"


def replies_report(jobs: Iterable[dict[str, Any]], contacts: Iterable[dict[str, Any]],
                   today: date, days: int = 30,
                   min_sample: int = DEFAULT_MIN_SAMPLE) -> str:
    """Where replies and interviews came from in the last `days` days: by board, by country
    and by the kind of mail (referral ask or cold mail), with a tip when a group of at least
    `min_sample` beats another. Pure; no AI."""
    stats = compute(jobs, contacts, today, days, f"Last {days} days")
    lines = [f"What gets replies (last {days} days, {stats.funnel.applied} applied, "
             f"{stats.funnel.replied} replied, {stats.funnel.interview} interviews):"]
    tips: list[str] = []
    for prop, name in REPORT_SPLITS:
        groups = stats.splits.get(prop) or {}
        if not groups:
            continue
        ordered = sorted(groups.items(), key=lambda kv: (-kv[1].rate, -kv[1].applied, kv[0]))
        lines.append(f"- By {name}: " + "; ".join(_group_line(k, f) for k, f in ordered))
        tip = _best_and_worst(groups, min_sample)
        if tip:
            tips.append(f"{name}: {tip}")
    mail = {MAIL_KIND.get(kind, kind): rate for kind, rate in stats.contacts.items()}
    if mail:
        ordered_mail = sorted(mail.items(), key=lambda kv: (-kv[1].rate, -kv[1].contacted))
        lines.append("- By mail: " + "; ".join(
            f"{kind} {r.replied}/{r.contacted} replied ({pct(r.rate)})"
            for kind, r in ordered_mail))
        fair = [(k, r) for k, r in ordered_mail if r.contacted >= min_sample]
        if len(fair) >= 2 and fair[0][1].rate > fair[-1][1].rate:
            tips.append(f"mail: {fair[0][0]} gets more replies ({pct(fair[0][1].rate)}) than "
                        f"{fair[-1][0]} ({pct(fair[-1][1].rate)})")
    if stats.funnel.applied == 0 and not mail:
        return f"What gets replies (last {days} days): nothing applied or mailed yet."
    if tips:
        lines.append("Put more effort where it works: " + "; ".join(tips) + ".")
    else:
        lines.append(f"Not enough data for a tip yet (needs at least {min_sample} in two "
                     "groups).")
    return "\n".join(lines)
