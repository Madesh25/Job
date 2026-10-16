"""Hunter: one domain search (type personal, IT/management/HR departments, limit 10), which
normally costs one credit. The API key is a query parameter, so it must never be logged:
jobengine.http only ever logs URLs without their query string.

Endpoint: GET https://api.hunter.io/v2/domain-search. Hunter has no per-person location, so
its candidates are marked country_unverified.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobengine import http
from jobengine.contacts.classify import classify_title, wide_kind
from jobengine.contacts.models import SLOT_TYPES, Candidate, ProviderResult
from jobengine.contacts.providers.base import ProviderDeps

NAME = "Hunter"
DOMAIN_SEARCH = "https://api.hunter.io/v2/domain-search"
DEPARTMENTS = "it,management,hr"
LIMIT = 10
# 16 Oct (strict location rule): Hunter's domain search filters people by country (a POST
# with a JSON `location`). Its people count as placed in the country only when Hunter's
# answer echoes the filter in meta.params; otherwise their location stays unknown.
COUNTRY_CODES = {"Poland": "PL", "Netherlands": "NL", "Ireland": "IE"}


def location_body(country: str) -> dict[str, Any] | None:
    code = COUNTRY_CODES.get(country)
    return {"location": {"include": [{"country": code}]}} if code else None


def _filtered(found: Mapping[str, Any]) -> bool:
    params = ((found.get("meta") or {}).get("params")) or {}
    return bool(params.get("location"))


def params(domain: str, key: str, department: str = DEPARTMENTS,
           offset: int = 0) -> dict[str, str | int]:
    found: dict[str, str | int] = {"domain": domain, "type": "personal",
                                   "department": department, "limit": LIMIT, "api_key": key}
    if offset:
        found["offset"] = offset
    return found


def _candidates(data: Mapping[str, Any], seen: set[str],
                country: str | None = None) -> list[Candidate]:
    """People from one answer; `country` when Hunter confirmed its country filter."""
    found = []
    for item in data.get("emails") or []:
        email = item.get("value") or ""
        if not email or item.get("type") == "generic" or email.lower() in seen:
            continue
        verification = (item.get("verification") or {}).get("status")
        if verification == "invalid":
            continue  # Hunter says this address bounces: never mail it
        seen.add(email.lower())
        first, last = item.get("first_name") or "", item.get("last_name") or ""
        name = " ".join(p for p in (first, last) if p) or email
        found.append(Candidate(
            name=name, first_name=first or name.split(" ")[0], title=item.get("position") or "",
            email=email, country=country, provider_verified=verification == "valid",
            source=NAME, country_unverified=country is None, department=item.get("department"),
        ))
    return found


def _get(deps: ProviderDeps, query: Mapping[str, Any], country: str) -> dict[str, Any]:
    """One domain search, with the country filter when the job is in one of our countries."""
    body = location_body(country)
    if body is None:
        return deps.request("GET", DOMAIN_SEARCH, params=query)
    return deps.request("POST", DOMAIN_SEARCH, params=query, json_body=body)


def _still_open(candidates: list[Candidate], open_slots: Mapping[str, int],
                deps: ProviderDeps) -> dict[str, int]:
    remaining = dict(open_slots)
    for c in candidates:
        kind = classify_title(c.title, deps.patterns)
        if kind not in remaining:
            kind = wide_kind(c.title, c.department)
        if remaining.get(kind, 0) > 0:
            remaining[kind] -= 1
    return remaining


def search(
    company: str, domain: str, country: str, open_slots: Mapping[str, int], deps: ProviderDeps,
) -> ProviderResult:
    key = deps.s.hunter_api_key
    if not key:
        return ProviderResult(skipped_reason="hunter: HUNTER_API_KEY not set")
    try:
        found = _get(deps, params(domain, key), country)
    except http.HttpError as exc:
        if exc.status in (401, 403):
            return ProviderResult(skipped_reason="hunter: not available on this plan")
        return ProviderResult(skipped_reason=f"hunter: {exc}")
    data = found.get("data") or {}
    seen: set[str] = set()
    placed = country if _filtered(found) else None
    result = ProviderResult(credits_used=1, candidates=_candidates(data, seen, placed))
    if location_body(country) and placed is None:
        result.notes.append(f"hunter: its answer did not confirm the {country} filter, so "
                            "its people count as location unknown")

    # 16 Oct: one page of 10 left the recruiter and a peer open at State Street. When slots
    # stay open: the HR department for the recruiter, and the next page for engineers. Each
    # extra search costs one credit and runs only while credits are left.
    extra = []
    if _still_open(result.candidates, open_slots, deps).get(SLOT_TYPES["recruiter"], 0) > 0:
        extra.append(("HR department", params(domain, key, department="hr")))
    open_now = _still_open(result.candidates, open_slots, deps)
    if (open_now.get(SLOT_TYPES["peer"], 0) or open_now.get(SLOT_TYPES["hiring"], 0)) and \
            len(data.get("emails") or []) >= LIMIT:
        extra.append(("next page", params(domain, key, offset=LIMIT)))
    for what, query in extra:
        if deps.credits_left - result.credits_used <= 0:
            result.notes.append(f"hunter: no credits left for the {what} search")
            break
        try:
            answer = _get(deps, query, country)
        except http.HttpError as exc:
            result.notes.append(f"hunter: {what} search failed: {exc}")
            continue
        result.credits_used += 1
        result.candidates.extend(_candidates(answer.get("data") or {}, seen,
                                             country if _filtered(answer) else None))
    return result

def probe(domain: str, deps: ProviderDeps) -> tuple[int, list[str]]:
    found = deps.request("GET", DOMAIN_SEARCH, params=params(domain, deps.s.hunter_api_key or ""))
    emails = (found.get("data") or {}).get("emails") or []
    return len(emails), sorted({k for e in emails for k in e})
