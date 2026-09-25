"""The Contacts cache (spec section 2, step 2): contacts already found at this company."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from jobengine.contacts.models import TYPE_ORDER, Chosen, Mix
from jobengine.sweep.normalize import canon_company

BLOCKED = frozenset({"Bounced", "Do not contact"})


def months_before(day: date, months: int) -> date:
    year, month = day.year, day.month - months
    while month <= 0:
        month += 12
        year -= 1
    for d in (day.day, 30, 29, 28):
        try:
            return date(year, month, d)
        except ValueError:
            continue
    return date(year, month, 28)


@dataclass(frozen=True)
class CachedRow:
    page_id: str
    values: dict[str, Any]

    @property
    def email(self) -> str:
        return (self.values.get("Email") or "").strip().lower()

    @property
    def blocked(self) -> bool:
        return self.values.get("Status") in BLOCKED

    def chosen(self) -> Chosen:
        v = self.values
        return Chosen(name=v.get("Name") or "", title=v.get("Title") or "",
                      email=v.get("Email") or "", type=v.get("Type") or "Other",
                      source=v.get("Source") or "", country=v.get("Country") or "",
                      verified=v.get("Status") == "Verified", page_id=self.page_id, cached=True)


def company_rows(rows: list[tuple[str, dict[str, Any]]], company: str) -> list[CachedRow]:
    key = canon_company(company)
    return [CachedRow(pid, v) for pid, v in rows if canon_company(v.get("Company")) == key]


def usable(row: CachedRow, today: date, months: int, job_id: str | None = None) -> bool:
    """Not Bounced or Do not contact, and found within `months` (or already on this job)."""
    if row.blocked:
        return False
    if job_id and job_id in (row.values.get("Related jobs") or []):
        return True
    found = row.values.get("Date found")
    return isinstance(found, date) and found >= months_before(today, months)


def pick(
    rows: list[CachedRow], mix: Mix, country: str, today: date, months: int,
    job_id: str | None = None,
) -> list[Chosen]:
    """Cached contacts for the mix: the job's country first, then the most recent."""
    slots = mix.slots()
    fresh = [r for r in rows if usable(r, today, months, job_id)]
    fresh.sort(key=lambda r: (
        (r.values.get("Country") or "") != country,
        -(r.values.get("Date found") or date.min).toordinal(),
    ))
    chosen: list[Chosen] = []
    for kind in TYPE_ORDER:
        for row in fresh:
            if slots[kind] <= 0:
                break
            if row.values.get("Type") == kind:
                chosen.append(row.chosen())
                slots[kind] -= 1
    return chosen
