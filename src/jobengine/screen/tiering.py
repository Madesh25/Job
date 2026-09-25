"""Survivors (spec section 6): evidence matrix, tier and rank."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from jobengine.reference import Backing, Reference
from jobengine.screen import visa
from jobengine.screen.models import Extraction, JobRow, MatrixItem, Requirement

TIERS = ("Needs review", "Apply low", "Apply normal", "Apply high")  # lowest first
LARGE_TIERS = frozenset({1, 2, 3, 4, 6})
SNIPPET_NOTE = "snippet only, paste the full JD with /jd"


def _strength(backings: list[Backing]) -> tuple[str, Backing | None]:
    for backing in backings:
        if backing.kind == "term_map" or backing.level == "Production":
            return "Strong", backing
    for backing in backings:
        if backing.level == "Hands-on":
            return "Transferable", backing
    return "Gap", None


def matrix_item(req: Requirement, ref: Reference, mandatory: bool) -> MatrixItem:
    terms = req.terms or [req.text]
    backings = [b for b in (ref.lookup(t) for t in terms) if b is not None]
    strength, backing = _strength(backings)
    return MatrixItem(strength=strength, text=req.text, backing=backing.name if backing else None,
                      mandatory=mandatory, kind=req.kind)


def build_matrix(ext: Extraction, ref: Reference) -> list[MatrixItem]:
    return [matrix_item(r, ref, True) for r in ext.mandatory_requirements] + [
        matrix_item(r, ref, False) for r in ext.nice_to_have
    ]


def gap_count(matrix: list[MatrixItem]) -> int:
    """Gaps across mandatory non-tool requirements and nice-to-haves (mandatory tool gaps
    never get here: gate 4 skips them)."""
    return sum(
        1 for item in matrix
        if item.strength == "Gap" and not (item.mandatory and item.kind == "tool")
    )


def contract_type(country: str | None, ext: Extraction) -> str:
    """Job Opportunities "Contract type": UoP, B2B, Both or Unknown (Poland only)."""
    if country != "Poland":
        return "Unknown"
    values = ext.contract_values
    uop, b2b = "UoP" in values, "B2B" in values
    if uop and b2b:
        return "Both"
    return "UoP" if uop else "B2B" if b2b else "Unknown"


def permanent_contract(country: str | None, ext: Extraction) -> bool:
    if country == "Poland":
        return contract_type(country, ext) in ("UoP", "Both")
    values = ext.contract_values
    return "permanent" in values or "contract" not in values


def employer_size(row: JobRow, ext: Extraction, ref: Reference, flags: list[str]) -> str:
    """Config employer.size_rules: large, weak or normal."""
    company = ref.company(row.company)
    if (company and company.tier_number in LARGE_TIERS) or visa.ON_IND in flags:
        return "large"
    if (company is None and ext.sponsorship_value != "stated_yes") or (
        visa.AGENCY_IE in flags or visa.NOT_ON_IND in flags
    ):
        return "weak"
    return "normal"


def _up(tier: str) -> str:
    return TIERS[min(TIERS.index(tier) + 1, len(TIERS) - 1)] if tier != "Needs review" else tier


def _cap(tier: str, cap: str) -> str:
    return tier if TIERS.index(tier) <= TIERS.index(cap) else cap


@dataclass(frozen=True)
class Tiering:
    verdict: str
    bottom: bool
    employer: str
    gap_count: int
    flags: tuple[str, ...]  # flags added by tiering (Sponsorship stated)
    notes: tuple[str, ...]


def tier(
    row: JobRow,
    ext: Extraction,
    ref: Reference,
    matrix: list[MatrixItem],
    flags: list[str],
    description_kind: str,
) -> Tiering:
    """Section 6.2: base tier from gaps, employer and contract, then the three adjustments."""
    gaps = gap_count(matrix)
    employer = employer_size(row, ext, ref, flags)
    if gaps == 0 and employer == "large" and permanent_contract(row.country, ext):
        verdict = "Apply high"
    elif gaps <= 1:
        verdict = "Apply normal"
    elif gaps == 2:
        verdict = "Apply low"
    else:
        verdict = "Needs review"

    added: list[str] = []
    notes: list[str] = []
    if ext.sponsorship_value == "stated_yes":
        verdict = _up(verdict)
        added.append(visa.SPONSORSHIP_STATED)
    if employer == "weak":
        verdict = _cap(verdict, "Apply low")
    bottom = False
    if ext.sponsorship_value == "stated_no" or visa.NOT_ON_IND in flags or visa.AGENCY_IE in flags:
        # "Set Apply low": a Needs review row stays Needs review, it is never promoted.
        verdict = _cap(verdict, "Apply low")
        bottom = True
    if description_kind == "snippet":
        verdict = "Needs review"
        notes.append(SNIPPET_NOTE)
    return Tiering(verdict, bottom, employer, gaps, tuple(added), tuple(notes))


def rank_key(
    verdict: str,
    bottom: bool,
    row: JobRow,
    ext: Extraction | None,
    ref: Reference,
    today: date,
) -> tuple[int, int, int]:
    """Sort key for /pending, highest first (use with reverse=True)."""
    score = 0
    years = row.years_required if row.years_required is not None else (ext.years if ext else None)
    if years is None:
        score += 1
    elif 2 <= years <= 4:
        score += 3
    elif years == 5:
        score -= 2
    if ext and ext.sponsorship_value == "stated_yes":
        score += 3
    company = ref.company(row.company)
    if company and company.tier_number in LARGE_TIERS:
        score += 2
    score += {"High": -5, "Medium": -2}.get(row.ghost_risk or "", 0)
    if row.posted_date and 0 <= (today - row.posted_date).days <= 7:
        score += 1
    tier_index = TIERS.index(verdict) if verdict in TIERS else -1
    return (tier_index, 0 if bottom else 1, score)
