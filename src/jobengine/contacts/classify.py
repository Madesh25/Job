"""Classify, filter and dedupe candidates (spec section 4). Pure."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from jobengine.contacts.models import OTHER, SLOT_TYPES, Candidate, Mix

PERSONAL_DOMAINS = frozenset(
    {"gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "icloud.com", "proton.me"}
)
DEFAULT_PATTERNS = {
    "recruiter": r"recruit|talent acquisition|sourcer|talent partner|people partner|"
                 r"hr business partner|rekrut",
    "hiring": r"engineering manager|head of (devops|platform|infrastructure|cloud|sre|engineering)|"
              r"director of (engineering|infrastructure|platform)|"
              r"(devops|platform|infrastructure|sre|cloud) (team )?(lead|manager)|"
              r"tech lead|team lead",
    "peer": r"devops|site reliability|sre|platform engineer|cloud engineer|infrastructure engineer|"
            r"systems engineer|kubernetes|release engineer|build engineer",
}
EMAIL_RE = re.compile(r"^[^@\s]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")


def classify_title(title: str | None, patterns: Mapping[str, str] | None = None) -> str:
    """Contact Type for a job title: recruiter, then hiring, then peer; else Other."""
    patterns = patterns or DEFAULT_PATTERNS
    text = (title or "").casefold()
    for key in ("recruiter", "hiring", "peer"):
        if patterns.get(key) and re.search(patterns[key], text):
            return SLOT_TYPES[key]
    return OTHER


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""


def valid_email(email: str | None) -> bool:
    return bool(email) and bool(EMAIL_RE.match(email.strip()))


def is_personal(email: str, personal: Iterable[str] = PERSONAL_DOMAINS) -> bool:
    return email_domain(email) in {d.lower() for d in personal}


def on_domain(email: str, domain: str) -> bool:
    got, want = email_domain(email), domain.strip().lower()
    return bool(want) and (got == want or got.endswith("." + want))


def country_ok(candidate: Candidate, country: str) -> str:
    """'in', 'unverified' or 'outside' for the job's country."""
    if candidate.country_unverified or not candidate.country:
        return "unverified"
    return "in" if candidate.country.casefold() == country.casefold() else "outside"


def keep(
    candidate: Candidate, domain: str | None, personal: Iterable[str] = PERSONAL_DOMAINS,
) -> bool:
    """Filter 1: no personal addresses; provider emails must be on the company domain.
    Job posting contacts are kept as written (except personal addresses)."""
    if not valid_email(candidate.email) or is_personal(candidate.email, personal):
        return False
    if candidate.source == "Job posting":
        return True
    return bool(domain) and on_domain(candidate.email, domain or "")


def fill_slots(
    candidates: list[Candidate],
    open_slots: Mapping[str, int],
    country: str,
    domain: str | None,
    seen: set[str],
    patterns: Mapping[str, str] | None = None,
    personal: Iterable[str] = PERSONAL_DOMAINS,
) -> list[tuple[Candidate, str, str]]:
    """Choose candidates for the open slots: (candidate, type, note).

    In-country first, then country-unverified, and only then people outside the country
    ("outside <country>"). `seen` holds lowercase emails already used (cache and this run)
    and is updated. Candidates beyond the open slots are dropped.
    """
    remaining = dict(open_slots)
    ranked: dict[str, list[tuple[int, Candidate]]] = {t: [] for t in remaining}
    order = {"in": 0, "unverified": 1, "outside": 2}
    for index, candidate in enumerate(candidates):
        if not keep(candidate, domain, personal):
            continue
        kind = classify_title(candidate.title, patterns)
        if kind not in ranked:
            continue
        ranked[kind].append((order[country_ok(candidate, country)] * 10_000 + index, candidate))
    chosen: list[tuple[Candidate, str, str]] = []
    for kind, items in ranked.items():
        for _, candidate in sorted(items, key=lambda x: x[0]):
            if remaining.get(kind, 0) <= 0:
                break
            key = candidate.email.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            where = country_ok(candidate, country)
            note = {"outside": f"outside {country}", "unverified": "country unverified"}.get(
                where, "")
            chosen.append((candidate, kind, note))
            remaining[kind] -= 1
    return chosen


MIX_PART = re.compile(r"(peer|hiring|recruiter)\s*=\s*(\d+)", re.IGNORECASE)


def parse_mix(value: str | None, per_job: str | None = None) -> Mix:
    """Config contacts.mix ("peer=2, hiring=1, recruiter=1") and contacts.per_job.
    The target total is max(per_job, sum of the mix). Unreadable: the 2/1/1 default."""
    parts = {k.lower(): int(v) for k, v in MIX_PART.findall(value or "")}
    try:
        total = int(str(per_job).strip()) if per_job else 0
    except ValueError:
        total = 0
    if set(parts) != {"peer", "hiring", "recruiter"}:
        return Mix(total=max(total, 4),
                   note="Config contacts.mix could not be read, used peer 2, hiring 1, recruiter 1")
    return Mix(peer=parts["peer"], hiring=parts["hiring"], recruiter=parts["recruiter"],
               total=max(total, sum(parts.values())))
