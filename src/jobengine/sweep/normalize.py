"""Normalising and scoping postings (spec section 4). Pure functions, no I/O."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from jobengine.sweep.models import Job, RawPosting, Skipped

# Legal suffixes removed from the end of company names, in canonical form.
COMPANY_SUFFIXES = (
    "sp z o o",
    "s a",
    "b v",
    "n v",
    "ltd",
    "limited",
    "inc",
    "gmbh",
    "plc",
    "llc",
    "group",
)

# Canonical city aliases used in dedupe keys.
CITY_ALIASES = {
    "warsaw": "warszawa",
    "the hague": "den haag",
    "s gravenhage": "den haag",
    "cracow": "krakow",
}

# Gender markers and bracketed extras removed from titles before building keys.
_MARKER = r"(?:[mfwdkx]\s*/\s*[mfwdkx](?:\s*/\s*[mfwdkx])?|remote|hybrid|on-?site)"
_BRACKETED_MARKER = re.compile(rf"[\(\[]\s*{_MARKER}\s*[\)\]]", re.IGNORECASE)
_BARE_GENDER = re.compile(r"\b[mfwdkx]/[mfwdkx](?:/[mfwdkx])?\b", re.IGNORECASE)

_SENIORITY = (
    ("Junior", re.compile(r"\b(junior|jr)\b")),
    ("Mid", re.compile(r"\b(mid|regular|medior)\b")),
    ("Senior", re.compile(r"\b(senior|sr)\b")),
)

_DASH = r"[-\u2013\u2014]"


def canon(text: str | None) -> str:
    """Lowercase ASCII with accents stripped, only a-z 0-9 + # and single spaces."""
    if not text:
        return ""
    text = text.replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9+# ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canon_company(name: str | None) -> str:
    value = canon(name)
    changed = True
    while changed:
        changed = False
        for suffix in COMPANY_SUFFIXES:
            if value.endswith(" " + suffix):
                value = value[: -len(suffix) - 1].strip()
                changed = True
    return value


def clean_title(title: str) -> str:
    """Title without gender markers and bracketed extras, original casing kept."""
    title = _BRACKETED_MARKER.sub(" ", title)
    title = _BARE_GENDER.sub(" ", title)
    return re.sub(r"\s+", " ", title).strip(" -|,")


def canon_title(title: str | None) -> str:
    return canon(clean_title(title or ""))


def canon_city(city: str | None) -> str:
    value = canon(city)
    return CITY_ALIASES.get(value, value)


def dedupe_key(company: str, title: str, city: str | None, country: str) -> str:
    """V16 company-role-city key, all parts canonical."""
    place = canon_city(city) if city else canon(country)
    return f"{canon_company(company)}|{canon_title(title)}|{place}"


def _has_term(text: str, terms: Iterable[str]) -> str | None:
    for term in terms:
        if re.search(rf"(?:^| ){re.escape(canon(term))}(?: |$)", text):
            return term
    return None


def seniority(title: str) -> str:
    value = canon(title)
    for label, pattern in _SENIORITY:
        if pattern.search(value):
            return label
    return "Unknown"


_RANGE = re.compile(rf"(\d{{1,2}})\s*{_DASH}\s*(\d{{1,2}})\s*(?:years?|yrs?|lat[a]?)\b")
_PLUS = (
    re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?|lat[a]?)\b"),
    re.compile(r"at\s+least\s+(\d{1,2})\s*(?:years?|yrs?)\b"),
    re.compile(r"minimum(?:\s+of)?\s+(\d{1,2})\s*(?:years?|yrs?)\b"),
    re.compile(r"min\.?\s*(\d{1,2})\s*(?:lat[a]?|years?|yrs?)\b"),
)
MAX_STATED_YEARS = 15  # "since 18 years", "25+ years of history" are about the company
# A requirement is one line or sentence of the description ("min. 4 lat" is not a sentence).
_SEGMENT = re.compile(
    r"\n|<br\s*/?>|;|\u2022|(?<=[a-z0-9)])(?<!\bmin)(?<!\bnp)(?<!\bca)(?<!\bapprox)\.\s"
)


def segments(text: str) -> list[str]:
    """The lines and sentences of a description, lowercase."""
    return _SEGMENT.split(text.lower())
_OPTIONAL = re.compile(
    r"nice[\s-]to[\s-]have|\ba plus\b|\bpreferred\b|\bpreferabl|\bideally\b|\badvantage"
    r"|\bbonus\b|\bdesirable\b|mile widziane|\batut|pluspunt|\been pr[eé]\b"
)
_EXPERIENCE_WORD = re.compile(r"experience|\bexp\b|do[sś]wiadcz|ervaring")
_YEARS_OF = re.compile(r"\b(\d{1,2})\s+years?\s+(?:of\s+)?(?:experience|exp)\b")
_BARE_LAT = re.compile(r"\b(\d{1,2})\s+lat[a]?\b")  # "5 lat" alone is often "for 5 years"


def _year_options(description: str | None) -> list[tuple[int, str]]:
    """(lowest years, as posted) for each experience requirement in the description.

    Requirements in "nice to have" or "preferred" lines only count when nothing else is
    stated. A bare "5 lat" only counts on a line about experience (doswiadczenie)."""
    if not description:
        return []
    required: list[tuple[int, str]] = []
    optional: list[tuple[int, str]] = []
    for segment in segments(description):
        found: list[tuple[int, str]] = []
        for m in _RANGE.finditer(segment):
            low, high = int(m.group(1)), int(m.group(2))
            if 0 < low < high <= 20:
                found.append((low, f"{low}-{high} years"))
        for pattern in _PLUS:
            found.extend((int(m.group(1)), f"{int(m.group(1))}+ years")
                         for m in pattern.finditer(segment))
        if not found:
            patterns = [_YEARS_OF]
            if _EXPERIENCE_WORD.search(segment):
                patterns.append(_BARE_LAT)
            found.extend((int(m.group(1)), f"{int(m.group(1))} years")
                         for pattern in patterns for m in pattern.finditer(segment))
        found = [(low, label) for low, label in found if 0 < low <= MAX_STATED_YEARS]
        (optional if _OPTIONAL.search(segment) else required).extend(found)
    return required or optional


def experience(description: str | None) -> str | None:
    """The experience asked for, as the posting says it: "2-3 years", "5+ years" or
    "3 years". When several requirements are stated, the one asking for the most years
    ("5 years in IT, 2 years in a similar role" is 5 years), so the cell shows the real bar.
    """
    options = _year_options(description)
    if not options:
        return None
    top = max(low for low, _ in options)
    labels = [label for low, label in options if low == top]
    # "3-5 years" says more than "3+ years" for the same lowest number.
    return next((label for label in labels if "-" in label), labels[0])


NOT_STATED = "Not stated"

# The posting itself calls the role senior ("a hands-on senior engineering role"), as opposed
# to mentioning senior colleagues ("you will work with senior engineers").
_SENIOR_ROLE = re.compile(
    r"\bsenior[\s-]+level\b"
    r"|\bsenior (?:engineering |technical )?(?:role|position)\b"
    r"|\b(?:as|for|hiring|seeking|looking for|join us as) an? (?:\w+ ){0,2}senior\b"
    r"|\bthis (?:is an? )?(?:\w+ ){0,2}senior\b"
)


def senior_in_text(description: str | None) -> bool:
    """True when the description says the role itself is senior."""
    return bool(description and _SENIOR_ROLE.search(description.lower()))


def years_text(years: int | None, posted: str | None, full: bool = False) -> str | None:
    """The Years required cell: as posted ("2-3 years"), else the number, else "Not
    stated" when the full description was read and names no years, else empty."""
    if posted:
        return posted
    if years is not None:
        return f"{years} years"
    return NOT_STATED if full else None


def years_low(value: Any) -> int | None:
    """The lowest years in a Years required cell: 3 for "3-5 years" or for the number 3."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    match = re.search(r"\d{1,2}", str(value))
    return int(match.group(0)) if match else None


def years_required(description: str | None) -> int | None:
    """The years of experience the description requires: the highest of its requirements
    (each counted by its lowest number, "3-5 years" is 3), or None."""
    options = _year_options(description)
    return max(low for low, _ in options) if options else None


@dataclass(frozen=True)
class Rules:
    title_include: tuple[str, ...]
    title_exclude: tuple[str, ...]
    # country -> (canonical country names, {display city: canonical aliases})
    locations: Mapping[str, tuple[tuple[str, ...], Mapping[str, tuple[str, ...]]]]

    @classmethod
    def from_config(cls, sweep: Mapping[str, Any]) -> Rules:
        locations = {}
        for country, spec in (sweep.get("locations") or {}).items():
            names = tuple(canon(n) for n in spec.get("names") or [])
            cities = {
                city: tuple(canon(a) for a in aliases)
                for city, aliases in (spec.get("cities") or {}).items()
            }
            locations[country] = (names, cities)
        return cls(
            title_include=tuple(sweep.get("title_include") or ()),
            title_exclude=tuple(sweep.get("title_exclude") or ()),
            locations=locations,
        )


def title_scope(title: str, rules: Rules) -> str | None:
    """None when the title is in scope, otherwise the reason it is not."""
    value = canon_title(title)
    excluded = _has_term(value, rules.title_exclude)
    if excluded:
        return f"title excluded ({excluded})"
    if not _has_term(value, rules.title_include):
        return "title not in scope"
    return None


def _detect(text: str, rules: Rules) -> tuple[str | None, str | None]:
    value = canon(text)
    if not value:
        return None, None
    for country, (_, cities) in rules.locations.items():
        for city, aliases in cities.items():
            if _has_term(value, aliases):
                return country, city
    for country, (names, _) in rules.locations.items():
        if _has_term(value, names):
            return country, ("Remote" if _has_term(value, ("remote",)) else None)
    return None, None


def detect_location(
    location_text: str, rules: Rules, area: Iterable[str] = ()
) -> tuple[str | None, str | None]:
    """(country, city) from structured area parts first, then the free text."""
    found = [_detect(text, rules) for text in (" , ".join(area), location_text)]
    with_city = [(country, city) for country, city in found if country and city]
    if with_city:
        return with_city[0]
    for country, _ in found:
        if country:
            remote = _has_term(canon(location_text), ("remote",))
            return country, ("Remote" if remote else None)
    return None, None


def normalize(
    raw: RawPosting, rules: Rules, active_countries: Iterable[str]
) -> Job | Skipped:
    """Turn a raw posting into a Job, or Skipped when it is out of scope."""
    reason = title_scope(raw.title, rules)
    if reason:
        return Skipped(raw, reason)
    country, city = detect_location(raw.location_text, rules, raw.location_area)
    if country is None:
        return Skipped(raw, f"location not recognised ({raw.location_text or 'empty'})")
    if country not in set(active_countries):
        return Skipped(raw, f"country not active ({country})")
    return Job(
        source=raw.source,
        board=raw.board,
        company=raw.company.strip(),
        role=raw.title.strip(),
        city=city,
        country=country,
        url=raw.url,
        posted_date=raw.posted_date,
        salary=raw.salary_text or None,
        seniority=seniority(raw.title),
        years_required=years_required(raw.description),
        dedupe_key=dedupe_key(raw.company, raw.title, city, country),
        posting_ref=f"{raw.source}:{raw.posting_id}",
        description=raw.description or None,
        description_is_snippet=raw.description_is_snippet,
        experience=experience(raw.description),
    )


def parse_active_countries(value: str | None) -> list[str]:
    """Config countries.active ("Poland, Netherlands, Ireland") as a list."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]
