"""A research tip and the result of validating a batch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CATEGORIES = ("ATS", "Application", "Interview", "Outreach", "Resume", "Other")
COUNTRIES = ("Poland", "Netherlands", "Ireland", "All")


@dataclass
class Tip:
    tip: str
    category: str = "Other"
    countries: list[str] = field(default_factory=lambda: ["All"])
    why: str = ""
    sources: list[str] = field(default_factory=list)
    conflicts_with_v16: bool = False
    published: str = "unknown"

    @classmethod
    def from_raw(cls, raw: Any) -> Tip | None:
        """None when the item is not a usable tip (no tip text)."""
        if not isinstance(raw, dict):
            return None
        text = " ".join(str(raw.get("tip") or "").split())
        if not text:
            return None
        category = str(raw.get("category") or "Other").strip()
        countries = raw.get("countries") or ["All"]
        if isinstance(countries, str):
            countries = [countries]
        sources = raw.get("sources") or []
        if isinstance(sources, str):
            sources = [sources]
        return cls(
            tip=text,
            category=category if category in CATEGORIES else "Other",
            countries=[c for c in (str(x).strip() for x in countries) if c in COUNTRIES]
            or ["All"],
            why=" ".join(str(raw.get("why") or "").split()),
            sources=[str(u).strip() for u in sources if str(u).strip()],
            conflicts_with_v16=bool(raw.get("conflicts_with_v16")),
            published=str(raw.get("published") or "unknown"),
        )


@dataclass
class Checked:
    """Tips from one research run after validation."""

    kept: list[Tip] = field(default_factory=list)
    rejected: list[tuple[Tip, str]] = field(default_factory=list)  # (tip, reason)
    no_source: int = 0
    duplicates: int = 0
