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

# Years of experience. The first group is always the lower bound.
_DASH = r"[-\u2013\u2014]"
_YEARS = (
    re.compile(rf"(\d{{1,2}})\s*{_DASH}\s*\d{{1,2}}\s*(?:years?|yrs?|lat[a]?)\b"),
    re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?|lat[a]?)\b"),
    re.compile(r"at\s+least\s+(\d{1,2})\s*(?:years?|yrs?)\b"),
    re.compile(r"minimum(?:\s+of)?\s+(\d{1,2})\s*(?:years?|yrs?)\b"),
    re.compile(r"min\.?\s*(\d{1,2})\s*(?:lat[a]?|years?|yrs?)\b"),
    re.compile(r"\b(\d{1,2})\s+lat[a]?\b"),
    re.compile(r"\b(\d{1,2})\s+years?\s+(?:of\s+)?(?:experience|exp)\b"),
)


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


def years_required(description: str | None) -> int | None:
    """Smallest explicit years-of-experience number, or None."""
    if not description:
        return None
    text = description.lower()
    found = [int(m.group(1)) for pattern in _YEARS for m in pattern.finditer(text)]
    found = [n for n in found if 0 < n <= 20]
    return min(found) if found else None


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
    )


def parse_active_countries(value: str | None) -> list[str]:
    """Config countries.active ("Poland, Netherlands, Ireland") as a list."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]
