"""Data carried through screening."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- LLM extraction schema


class Quoted(BaseModel):
    value: Any = None
    quote: str | None = None


class Language(BaseModel):
    language: str
    level: str  # mandatory | preferred
    quote: str | None = None


class Requirement(BaseModel):
    text: str
    terms: list[str] = Field(default_factory=list)
    kind: str = "other"  # tool | practice | education | certification | other
    quote: str | None = None


class Extraction(BaseModel):
    """What the LLM found in the job description, after the quote check."""

    years_required_min: Quoted = Field(default_factory=Quoted)
    seniority_title_flag: Quoted = Field(default_factory=lambda: Quoted(value="none"))
    languages: list[Language] = Field(default_factory=list)
    contract_types: list[Quoted] = Field(default_factory=list)
    mandatory_requirements: list[Requirement] = Field(default_factory=list)
    nice_to_have: list[Requirement] = Field(default_factory=list)
    sponsorship: Quoted = Field(default_factory=lambda: Quoted(value="not_mentioned"))
    work_mode: Quoted = Field(default_factory=lambda: Quoted(value="unknown"))
    salary_text: Quoted = Field(default_factory=Quoted)
    expires: Quoted = Field(default_factory=Quoted)
    agency_posting: Quoted = Field(default_factory=Quoted)
    specific_details: list[str] = Field(default_factory=list)

    # Convenience accessors -------------------------------------------------

    @property
    def years(self) -> int | None:
        value = self.years_required_min.value
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def sponsorship_value(self) -> str:
        return self.sponsorship.value or "not_mentioned"

    @property
    def contract_values(self) -> set[str]:
        return {str(c.value) for c in self.contract_types if c.value and c.value != "unknown"}

    @property
    def expires_date(self) -> date | None:
        try:
            return date.fromisoformat(str(self.expires.value)[:10]) if self.expires.value else None
        except ValueError:
            return None


# ---------------------------------------------------------------- screening results


@dataclass(frozen=True)
class MatrixItem:
    strength: str  # Strong | Transferable | Gap
    text: str
    backing: str | None
    mandatory: bool
    kind: str


@dataclass
class ScreenResult:
    page_id: str
    verdict: str  # Apply high | Apply normal | Apply low | Needs review | Skip | Unscreened
    skip_reason: str | None = None
    matrix: list[MatrixItem] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    tech_terms: list[str] = field(default_factory=list)
    visa_flags: list[str] = field(default_factory=list)
    bottom: bool = False
    notes: list[str] = field(default_factory=list)
    extraction: Extraction | None = None
    employer: str | None = None  # large | normal | weak
    description_kind: str = "none"  # full | snippet | none
    llm_used: bool = True  # False when a free check skipped the row before the LLM

    @property
    def counts(self) -> dict[str, int]:
        out = {"Strong": 0, "Transferable": 0, "Gap": 0}
        for item in self.matrix:
            out[item.strength] += 1
        return out


@dataclass
class ScreenSummary:
    screened: int = 0
    by_verdict: dict[str, int] = field(default_factory=dict)
    # None: this run did not look at rows without a description (/screen collect).
    waiting_for_jd: int | None = 0
    errors: list[str] = field(default_factory=list)
    results: list[ScreenResult] = field(default_factory=list)
    usage_line: str | None = None  # "AI used: ... about $0.01" (llm.Usage.line)

    def add(self, result: ScreenResult) -> None:
        self.results.append(result)
        if result.verdict == "Unscreened":
            return
        self.screened += 1
        self.by_verdict[result.verdict] = self.by_verdict.get(result.verdict, 0) + 1

    def text(self) -> str:
        v = self.by_verdict
        line = (
            f"Screening done: {self.screened} screened ({v.get('Apply high', 0)} high, "
            f"{v.get('Apply normal', 0)} normal, {v.get('Apply low', 0)} low, "
            f"{v.get('Needs review', 0)} needs review, {v.get('Skip', 0)} skipped)"
        )
        line += "." if self.waiting_for_jd is None else f", {self.waiting_for_jd} waiting for JD."
        free = sum(1 for r in self.results if not r.llm_used and r.verdict == "Skip")
        if free:
            line += f" {free} skipped without the AI."
        extra = [self.usage_line] if self.usage_line else []
        return "\n".join([line, *extra, *self.errors])


# ---------------------------------------------------------------- Job Opportunities row


@dataclass(frozen=True)
class JobRow:
    """The Job Opportunities properties screening reads."""

    page_id: str
    company: str
    role: str
    country: str | None = None
    city: str | None = None
    dedupe_key: str | None = None
    status: str | None = None
    screen_verdict: str | None = None
    years_required: int | None = None
    expires: date | None = None
    posted_date: date | None = None
    ghost_risk: str | None = None
    salary: str | None = None
    visa_flags: tuple[str, ...] = ()
    url: str | None = None
    board: str | None = None
    posting_ids: str | None = None
    sponsorship: str | None = None  # Stated yes | Stated no | Not mentioned
    contract_type: str | None = None
    gaps: str | None = None
    swept_date: date | None = None


# ---------------------------------------------------------------- the Gaps column

GAP_SEPARATOR = "; "


def split_gaps(text: str | None) -> list[str]:
    """The gaps in a Gaps cell. Since 5 Oct they are separated by ";" because one gap can hold
    commas ("monitoring, logging, and observability"); older cells use ",". Each gap once."""
    raw = str(text or "")
    parts = raw.split(";") if ";" in raw else raw.split(",")
    return join_list([p.strip() for p in parts if p.strip()])


def join_list(gaps: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for gap in gaps:
        key = gap.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(gap.strip())
    return out


def join_gaps(gaps: list[str]) -> str:
    """A Gaps cell: each gap once (5 Oct test P7 showed one twice), ";" between them."""
    return GAP_SEPARATOR.join(join_list(gaps))
