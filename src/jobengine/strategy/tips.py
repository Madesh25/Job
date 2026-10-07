"""Adopted strategy tips put to use (7 Oct, your decision): V16 stays the main rules and never
changes; the tips you adopted in /update come on top of it, only where they do not break V16.

- Resume and ATS tips go to the AI that tailors each resume ("follow only where every rule
  still holds"); the V16 resume gate still checks the result, so a tip can never win.
- Application tips are shown in the Apply pack, Outreach tips under the mail drafts,
  Interview tips with a reply that may be about an interview. Advice only.
- Other tips stay in /rules.

A tip counts for a job when its countries (Strategy "Notes": "countries: Poland, ...") are
"All" or include the job's country. At most MAX_TIPS tips, newest first.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

MAX_TIPS = 10
RESUME = ("Resume", "ATS")
APPLICATION = ("Application",)
OUTREACH = ("Outreach",)
INTERVIEW = ("Interview",)
_COUNTRIES = re.compile(r"countries:\s*([^;]+)", re.I)


@dataclass(frozen=True)
class AdoptedTip:
    text: str
    category: str
    countries: tuple[str, ...]

    def fits(self, country: str | None) -> bool:
        return "All" in self.countries or not country or country in self.countries


def adopted(rows: Iterable[tuple[str, dict[str, Any]]]) -> list[AdoptedTip]:
    """The adopted tips of the Strategy table, newest first."""
    picked = [v for _, v in rows if v.get("Status") == "Adopted" and v.get("Tip / rule")]
    picked.sort(key=lambda v: v.get("Date added") or date.min, reverse=True)
    tips = []
    for v in picked:
        match = _COUNTRIES.search(v.get("Notes") or "")
        countries = tuple(c.strip() for c in match.group(1).split(",") if c.strip()) \
            if match else ("All",)
        tips.append(AdoptedTip(text=" ".join(str(v["Tip / rule"]).split()),
                               category=v.get("Category") or "Other",
                               countries=countries or ("All",)))
    return tips


def pick(tips: Iterable[AdoptedTip], categories: tuple[str, ...], country: str | None,
         limit: int = MAX_TIPS) -> list[str]:
    """The tip texts of these categories that fit the country, newest first."""
    return [t.text for t in tips if t.category in categories and t.fits(country)][:limit]


def lines(title: str, texts: list[str]) -> str:
    """A short block for a Telegram message, or "" when there is no tip."""
    if not texts:
        return ""
    return "\n".join([f"\n\U0001F4A1 {title} (your adopted tips; V16 rules come first):",
                      *(f"- {t}" for t in texts)])
