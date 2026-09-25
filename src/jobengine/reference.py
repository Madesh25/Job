"""Reference data (read only): Skills Inventory, Term Map and Target Companies.

Loaded once per run. `lookup(term)` says whether a job requirement term is backed by a real
skill (Production or Hands-on) or by an Active Term Map row. Learning skills, Term Map rows
that are not Active, and rows whose Truthful equivalent is "(none)" never back a term.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from jobengine.settings import ROOT_DIR, Settings
from jobengine.sweep.normalize import canon, canon_company

if TYPE_CHECKING:
    from jobengine.notion_repo import NotionClient

FIXTURES = ROOT_DIR / "fixtures" / "reference"
OWNED_LEVELS = ("Production", "Hands-on")
NONE_EQUIVALENT = "(none)"


def canon_term(term: str | None) -> str:
    """Canonical requirement term: "Terraform 1.5" -> "terraform", "CI/CD" -> "ci cd"."""
    tokens = canon(term).split()
    return " ".join(t for t in tokens if not re.fullmatch(r"v?\d+", t))


def split_terms(value: str | None) -> list[str]:
    """A Term Map "JD term" cell may hold several terms: "Go, Golang"."""
    return [part.strip() for part in re.split(r"[,;]", value or "") if part.strip()]


def company_key(name: str | None) -> str:
    # "HubSpot (IE)" and "HubSpot" are the same company for matching.
    return canon_company(re.sub(r"\([^)]*\)", " ", name or ""))


@dataclass(frozen=True)
class Skill:
    row_id: str
    name: str
    level: str | None
    category: str | None = None
    where_used: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class TermRow:
    row_id: str
    jd_terms: tuple[str, ...]
    truthful_equivalent: str
    status: str | None
    category: str | None = None
    notes: str = ""
    evidence: tuple[str, ...] = ()  # Evidence Library page IDs ("Backed by evidence")

    @property
    def is_gap(self) -> bool:
        return self.truthful_equivalent.strip().lower() == NONE_EQUIVALENT

    @property
    def backs(self) -> bool:
        return self.status == "Active" and not self.is_gap


@dataclass(frozen=True)
class Company:
    row_id: str
    name: str
    region: str | None = None
    tier: str | None = None
    ind_sponsor: str | None = None
    active: bool = True
    careers_url: str | None = None
    hub_city: str | None = None

    @property
    def tier_number(self) -> int | None:
        match = re.match(r"\s*Tier\s+(\d+)", self.tier or "")
        return int(match.group(1)) if match else None


@dataclass(frozen=True)
class Backing:
    kind: str  # "skill" or "term_map"
    level: str | None  # "Production" or "Hands-on" for skills, None for Term Map rows
    row_id: str
    name: str


@dataclass
class Reference:
    skills: list[Skill] = field(default_factory=list)
    term_map: list[TermRow] = field(default_factory=list)
    companies: list[Company] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._skills = {canon_term(s.name): s for s in self.skills}
        self._terms: dict[str, TermRow] = {}
        for row in self.term_map:
            for term in (*row.jd_terms, row.truthful_equivalent):
                key = canon_term(term)
                # An Active backing row wins over a gap or review row for the same term.
                if key and (key not in self._terms or row.backs):
                    self._terms[key] = row
        self._companies = {company_key(c.name): c for c in self.companies}

    # ------------------------------------------------------------ builders

    @classmethod
    def load(cls, client: NotionClient, s: Settings) -> Reference:
        from jobengine.notion_repo import page_values

        def rows(name: str, props: tuple[str, ...]):
            for page in client.query(s.notion_read[name], properties=props):
                yield page["id"], page_values(page)

        skills = [
            Skill(row_id=pid, name=v.get("Skill") or "", level=v.get("Level"),
                  category=v.get("Category"), where_used=tuple(v.get("Where used") or ()),
                  notes=v.get("Notes") or "")
            for pid, v in rows("skills_inventory",
                               ("Skill", "Level", "Category", "Where used", "Notes"))
            if v.get("Skill")
        ]
        term_map = [
            TermRow(row_id=pid, jd_terms=tuple(split_terms(v.get("JD term"))),
                    truthful_equivalent=v.get("Truthful equivalent") or "",
                    status=v.get("Status"), category=v.get("Category"),
                    notes=v.get("Notes") or "",
                    evidence=tuple(v.get("Backed by evidence") or ()))
            for pid, v in rows("term_map", ("JD term", "Truthful equivalent", "Status",
                                            "Category", "Notes", "Backed by evidence"))
            if v.get("JD term")
        ]
        companies = [
            Company(row_id=pid, name=v.get("Company") or "", region=v.get("Region"),
                    tier=v.get("Tier"), ind_sponsor=v.get("IND sponsor"),
                    active=bool(v.get("Active")), careers_url=v.get("Careers URL"),
                    hub_city=v.get("Hub / City"))
            for pid, v in rows("target_companies", ("Company", "Region", "Tier", "IND sponsor",
                                                    "Active", "Careers URL", "Hub / City"))
            if v.get("Company")
        ]
        return cls(skills, term_map, companies)

    @classmethod
    def fake(cls, base: Path = FIXTURES) -> Reference:
        def load(name: str) -> list[dict]:
            return json.loads((base / name).read_text(encoding="utf-8"))

        skills = [
            Skill(row_id=r["id"], name=r["Skill"], level=r.get("Level"),
                  category=r.get("Category"), where_used=tuple(r.get("Where used") or ()),
                  notes=r.get("Notes") or "")
            for r in load("skills.json")
        ]
        term_map = [
            TermRow(row_id=r["id"], jd_terms=tuple(split_terms(r["JD term"])),
                    truthful_equivalent=r.get("Truthful equivalent") or "",
                    status=r.get("Status"), category=r.get("Category"),
                    notes=r.get("Notes") or "",
                    evidence=tuple(r.get("Backed by evidence") or ()))
            for r in load("term_map.json")
        ]
        companies = [
            Company(row_id=r["id"], name=r["Company"], region=r.get("Region"),
                    tier=r.get("Tier"), ind_sponsor=r.get("IND sponsor"),
                    active=r.get("Active", True), careers_url=r.get("Careers URL"),
                    hub_city=r.get("Hub / City"))
            for r in load("target_companies.json")
        ]
        return cls(skills, term_map, companies)

    # ------------------------------------------------------------ helpers

    def owned_terms(self) -> set[str]:
        """Lowercase terms that count as owned: Production and Hands-on skills, plus JD terms
        and truthful equivalents of Active Term Map rows. "(none)" rows are known gaps."""
        owned = {s.name.lower() for s in self.skills if s.level in OWNED_LEVELS}
        for row in self.term_map:
            if row.backs:
                owned.update(t.lower() for t in row.jd_terms)
                owned.add(row.truthful_equivalent.lower())
        return owned

    def lookup(self, term: str) -> Backing | None:
        key = canon_term(term)
        if not key:
            return None
        skill = self._skills.get(key)
        if skill and skill.level in OWNED_LEVELS:
            return Backing(kind="skill", level=skill.level, row_id=skill.row_id, name=skill.name)
        row = self._terms.get(key)
        if row and row.backs:
            return Backing(kind="term_map", level=None, row_id=row.row_id,
                           name=row.truthful_equivalent)
        return None

    def is_known_gap(self, term: str) -> bool:
        row = self._terms.get(canon_term(term))
        return bool(row and row.is_gap)

    def skill_level(self, term: str) -> str | None:
        """The Skills Inventory level for a term, whatever it is (Learning included)."""
        skill = self._skills.get(canon_term(term))
        return skill.level if skill else None

    def term_row(self, term: str) -> TermRow | None:
        return self._terms.get(canon_term(term))

    def evidence_for(self, terms: list[str]) -> list[str]:
        """Evidence Library page IDs linked from the Active Term Map rows behind `terms`."""
        out: list[str] = []
        for term in terms:
            row = self.term_row(term)
            if row and row.backs:
                out.extend(e for e in row.evidence if e not in out)
        return out

    def company(self, name: str | None) -> Company | None:
        return self._companies.get(company_key(name)) if name else None
