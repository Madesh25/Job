"""Classify, filter and dedupe candidates (spec section 4). Pure."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from jobengine.contacts.models import OTHER, SLOT_TYPES, Candidate, Mix

PERSONAL_DOMAINS = frozenset(
    {"gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "icloud.com", "proton.me"}
)
DEFAULT_PATTERNS = {
    # HR counts as recruiter: they get the cold mail, never the referral ask.
    "recruiter": r"recruit|talent acquisition|sourcer|talent partner|people partner|"
                 r"hr business partner|rekrut|\bhr\b|human resources|people (operations|ops)|"
                 r"talent (manager|lead|specialist)|kadr",
    "hiring": r"engineering manager|head of (devops|platform|infrastructure|cloud|sre|engineering)|"
              r"director of (engineering|infrastructure|platform)|"
              r"(devops|platform|infrastructure|sre|cloud) (team )?(lead|manager)|"
              r"tech lead|team lead",
    "peer": r"devops|site reliability|sre|platform engineer|cloud engineer|infrastructure engineer|"
            r"systems engineer|kubernetes|release engineer|build engineer",
}
# Wider search (16 Oct): when the usual titles leave slots open, senior people in tech at the
# same company count too ("Vice President, Cloud Engineering" at a bank; a senior engineer
# whose title names no DevOps word). A tech word or a tech department is always required, so
# a sales director never fills a slot.
WIDE_TECH = re.compile(r"engineer|technolog|technical|\bit\b|infrastructure|cloud|platform|"
                       r"devops|software|systems|architect|data|security|network|developer|"
                       r"development|\bsre\b|reliability|automation", re.I)
WIDE_LEAD = re.compile(r"\b(head|director|vice president|vp|svp|avp|chief|cto|cio|manager)\b",
                       re.I)
WIDE_SENIOR = re.compile(r"\b(senior|sr|lead|principal|staff|architect|expert)\b", re.I)
TECH_DEPARTMENTS = frozenset({"it", "engineering", "engineering & technical",
                              "information technology"})
HR_DEPARTMENTS = frozenset({"hr", "human resources"})
WIDE_NOTE = "senior, wider search"
# 22 Oct (your rule): junior people are never contacts; they cannot refer or hire.
JUNIOR = re.compile(r"\b(junior|jr|intern|internship|trainee|graduate|apprentice|student|"
                    r"werkstudent|sta[zż]ysta|praktykant|stagiair)\b", re.I)


# 22 Oct (O-15, Murata): "Legal Team Leader" took the Hiring slot through "team lead", and the
# wider search took "SCM Senior Project Engineer" through "engineer". Your rule is tech,
# managers and HR: a title naming a field outside IT never fills a peer or hiring slot (HR
# still fills the recruiter slot). Words that IT titles also use (production, quality,
# communications, scm, product) are left out on purpose.
NON_IT = re.compile(r"\b(legal|counsel|lawyer|attorney|paralegal|finance|financial|"
                    r"accountant|accounting|tax|treasury|sales|account (manager|executive)|"
                    r"business development|marketing|brand|procurement|purchasing|buyer|"
                    r"supply chain|logistics|warehouse|manufacturing|process engineer|"
                    r"project engineer|mechanical|electrical|electronics|hardware|analog|"
                    r"chemical|civil|construction|facilities|facility|maintenance|"
                    r"field service|customer service|office manager|executive assistant)\b",
                    re.I)


def is_junior(title: str | None) -> bool:
    return bool(JUNIOR.search(title or ""))


def is_non_it(title: str | None) -> bool:
    return bool(NON_IT.search(title or ""))


def left_slots(slots: Mapping[str, int], taken: Iterable[str], flexible: bool) -> dict[str, int]:
    """Open slots after the people already taken. `flexible` (Config contacts.roles any): a
    type may take another type's slot, so only the total counts; the slot taken back is the
    one with the most room."""
    remaining = dict(slots)
    for kind in taken:
        if remaining.get(kind, 0) > 0:
            remaining[kind] -= 1
        elif flexible and remaining and max(remaining.values()) > 0:
            donor = max(remaining, key=lambda k: remaining[k])
            remaining[donor] -= 1
    return {k: v for k, v in remaining.items() if v > 0}
EMAIL_RE = re.compile(r"^[^@\s]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")


def classify_title(title: str | None, patterns: Mapping[str, str] | None = None) -> str:
    """Contact Type for a job title: recruiter, then hiring, then peer; else Other."""
    patterns = patterns or DEFAULT_PATTERNS
    text = (title or "").casefold()
    for key in ("recruiter", "hiring", "peer"):
        if key != "recruiter" and is_non_it(text):
            return OTHER
        if patterns.get(key) and re.search(patterns[key], text):
            return SLOT_TYPES[key]
    return OTHER


def wide_kind(title: str | None, department: str | None = None) -> str:
    """The slot a senior person fills in the wider search: hiring (head, director, VP,
    manager), peer (senior, lead, principal, staff, architect) or recruiter (HR department);
    Other without a tech word or tech department."""
    text, dept = title or "", (department or "").strip().casefold()
    if dept in HR_DEPARTMENTS and text:
        return SLOT_TYPES["recruiter"]
    if is_non_it(text) or not (WIDE_TECH.search(text) or dept in TECH_DEPARTMENTS):
        return OTHER
    if WIDE_LEAD.search(text):
        return SLOT_TYPES["hiring"]
    if WIDE_SENIOR.search(text):
        return SLOT_TYPES["peer"]
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
    strict: bool = False,
    left_out: list[Candidate] | None = None,
    flexible: bool = False,
) -> list[tuple[Candidate, str, str]]:
    """Choose candidates for the open slots: (candidate, type, note).

    In-country first, then country-unverified, and only then people outside the country
    ("outside <country>"). People whose title fits no slot fill one only as senior tech
    people (wide_kind, note "senior, wider search"), after everyone who fits. `seen` holds
    lowercase emails already used (cache and this run) and is updated. Candidates beyond the
    open slots are dropped. `strict` (Config contacts.location): only people placed in the
    job's country (or named in the job posting) count; the others go to `left_out`.
    `flexible` (Config contacts.roles any, 22 Oct): after each type's own slots, the best
    people left of any type fill the slots still open. Junior people are always skipped.
    """
    remaining = dict(open_slots)
    ranked: dict[str, list[tuple[int, Candidate]]] = {t: [] for t in remaining}
    wider: set[int] = set()
    order = {"in": 0, "unverified": 1, "outside": 2}
    for index, candidate in enumerate(candidates):
        if not keep(candidate, domain, personal) or is_junior(candidate.title):
            continue
        if strict and candidate.source != "Job posting" and \
                country_ok(candidate, country) != "in":
            if left_out is not None:
                left_out.append(candidate)
            continue
        kind, wide = classify_title(candidate.title, patterns), 0
        if kind not in ranked:
            kind, wide = wide_kind(candidate.title, candidate.department), 100_000
            if kind not in ranked:
                continue
            wider.add(id(candidate))
        ranked[kind].append((wide + order[country_ok(candidate, country)] * 10_000 + index,
                             candidate))
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
            if id(candidate) in wider:
                note = f"{WIDE_NOTE}, {note}" if note else WIDE_NOTE
            chosen.append((candidate, kind, note))
            remaining[kind] -= 1
    if flexible and sum(v for v in remaining.values() if v > 0):
        # Any mix: the slots still open go to the best people left, whatever their type.
        rest = sorted(((rank, kind, c) for kind, items in ranked.items() for rank, c in items
                       if c.email.strip().lower() not in seen), key=lambda x: x[0])
        for _, kind, candidate in rest:
            room = {k: v for k, v in remaining.items() if v > 0}
            if not room:
                break
            key = candidate.email.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            where = country_ok(candidate, country)
            note = {"outside": f"outside {country}", "unverified": "country unverified"}.get(
                where, "")
            if id(candidate) in wider:
                note = f"{WIDE_NOTE}, {note}" if note else WIDE_NOTE
            chosen.append((candidate, kind, note))
            remaining[max(room, key=lambda k: room[k])] -= 1
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
