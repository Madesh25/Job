"""Data carried through a resume build."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class SkillRow:
    label: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class Bullet:
    id: str  # j<job index>b<bullet index>, for example j0b2
    job: int
    company: str
    text: str  # plain text (HTML entities decoded)


@dataclass(frozen=True)
class Profile:
    """Config profile.* values: the truth for the header links."""

    email: str
    phone: str
    website_url: str
    github: str
    linkedin_url: str
    relocation_line: str | None = None

    @property
    def tel(self) -> str:
        return "tel:" + "".join(ch for ch in self.phone if ch.isdigit() or ch == "+")

    def links(self) -> list[str]:
        """The 5 header link targets in their fixed order."""
        return [f"mailto:{self.email}", self.tel, self.website_url, self.github,
                self.linkedin_url]


@dataclass
class MasterResume:
    html: str  # the master with profile values filled in
    frozen_html: str  # html with {{SKILLS_ZONE}} and {{BULLET:<id>}} placeholders
    skills_main: list[SkillRow]
    skills_also: list[SkillRow]
    subhead_html: str  # the "Also worked with" paragraph, kept as is
    bullets: list[Bullet]
    companies: list[str]  # per job index
    version: str  # sha256 of the Golden Master HTML from Notion, first 8 hex
    cert_block: str | None = None  # the Certifications block (format only)

    def bullet(self, bullet_id: str) -> Bullet | None:
        return next((b for b in self.bullets if b.id == bullet_id), None)

    def all_items(self) -> list[str]:
        return [item for row in self.skills_main + self.skills_also for item in row.items]


# ---------------------------------------------------------------- the tailoring plan


class PlanRow(BaseModel):
    label: str
    items: list[str]


class BulletEdit(BaseModel):
    id: str
    text: str


class TermMapping(BaseModel):
    jd_term: str
    used_as: str
    where: str = ""


class Priority(BaseModel):
    bullet_edits: list[str] = Field(default_factory=list)  # most important first
    skill_items_low_relevance: list[str] = Field(default_factory=list)  # least relevant first


class Focus(BaseModel):
    skills: str = ""
    experience: str = ""
    summary: str = "unchanged"


class Plan(BaseModel):
    skills_main: list[PlanRow]
    skills_also: list[PlanRow]
    bullet_edits: list[BulletEdit] = Field(default_factory=list)
    term_mappings: list[TermMapping] = Field(default_factory=list)
    priority: Priority = Field(default_factory=Priority)
    focus: Focus = Field(default_factory=Focus)
    gaps_reported: list[str] = Field(default_factory=list)
    correction_refused: str | None = None

    @classmethod
    def unchanged(cls, master: MasterResume) -> Plan:
        """The plan that renders the master as it is."""
        return cls(
            skills_main=[PlanRow(label=r.label, items=list(r.items)) for r in master.skills_main],
            skills_also=[PlanRow(label=r.label, items=list(r.items)) for r in master.skills_also],
        )

    def edit_for(self, bullet_id: str) -> str | None:
        return next((e.text for e in self.bullet_edits if e.id == bullet_id), None)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


@dataclass
class Measure:
    pages: int
    fill: float
    text: str
    links: list[str]
    fonts: set[str] = field(default_factory=set)
