"""Gates (spec section 5): evaluated in V16 order, the first one that fires skips the row."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from jobengine.reference import Reference
from jobengine.screen.models import Extraction, JobRow
from jobengine.sweep.normalize import canon, canon_company, canon_title

MAX_YEARS = 5
SENIOR_TITLE = re.compile(r"\b(lead|principal|head of|staff)\b")
SKIP_LANGUAGES = {
    "polish": "Polish required",
    "polski": "Polish required",
    "dutch": "Dutch required",
    "nederlands": "Dutch required",
}
APPLIED_STATUSES = frozenset(
    {"Applied", "Followed up", "Replied", "Screening", "Interview", "Offer", "Rejected"}
)


@dataclass(frozen=True)
class GateHit:
    gate: int
    reason: str  # a Job Opportunities "Skip reason" option
    detail: str
    gaps: tuple[str, ...] = ()


def gate_seniority(row: JobRow, ext: Extraction) -> GateHit | None:
    flag = ext.seniority_title_flag.value
    if flag in ("lead", "principal"):
        return GateHit(1, "Seniority", f"description says {flag}")
    match = SENIOR_TITLE.search(canon(row.role))
    if match:
        return GateHit(1, "Seniority", f"title says {match.group(1)}")
    for years in (row.years_required, ext.years):
        if years is not None and years > MAX_YEARS:
            return GateHit(1, "Experience >5 yrs", f"{years} years required")
    return None


def gate_language(ext: Extraction) -> GateHit | None:
    for lang in ext.languages:
        if lang.level != "mandatory":
            continue
        reason = SKIP_LANGUAGES.get(canon(lang.language).split(" ")[0] if lang.language else "")
        if reason:
            return GateHit(2, reason, f"{lang.language} is mandatory")
    return None


def gate_b2b_only(row: JobRow, ext: Extraction) -> GateHit | None:
    if row.country == "Poland" and ext.contract_values == {"B2B"}:
        return GateHit(3, "B2B only", "only a B2B contract is offered")
    return None


def unbacked_tools(ext: Extraction, ref: Reference) -> list[str]:
    """Terms of mandatory tool requirements where no term is backed by the reference data."""
    missing: list[str] = []
    for req in ext.mandatory_requirements:
        if req.kind != "tool":
            continue
        terms = req.terms or [req.text]
        if not any(ref.lookup(term) for term in terms):
            missing.extend(t for t in terms if t not in missing)
    return missing


def gate_tech(ext: Extraction, ref: Reference) -> GateHit | None:
    missing = unbacked_tools(ext, ref)
    if missing:
        return GateHit(4, "Tech mismatch", "not backed: " + ", ".join(missing), tuple(missing))
    return None


def gate_already_applied(row: JobRow, others: Iterable[JobRow]) -> GateHit | None:
    company, role = canon_company(row.company), canon_title(row.role)
    for other in others:
        if other.page_id == row.page_id or other.status not in APPLIED_STATUSES:
            continue
        same_key = bool(row.dedupe_key) and other.dedupe_key == row.dedupe_key
        same_job = canon_company(other.company) == company and canon_title(other.role) == role
        if same_key or same_job:
            return GateHit(5, "Already applied", f"status {other.status} on another row")
    return None


def gate_expired(row: JobRow, ext: Extraction, today: date) -> GateHit | None:
    for expires in (ext.expires_date, row.expires):
        if expires is not None and expires < today:
            return GateHit(6, "Expired", f"expired {expires.isoformat()}")
    return None


def run_gates(
    row: JobRow, ext: Extraction, ref: Reference, others: Iterable[JobRow], today: date,
) -> GateHit | None:
    """The first gate that fires, in V16 order, or None when the row survives."""
    return (
        gate_seniority(row, ext)
        or gate_language(ext)
        or gate_b2b_only(row, ext)
        or gate_tech(ext, ref)
        or gate_already_applied(row, others)
        or gate_expired(row, ext, today)
    )
