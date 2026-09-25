"""Apollo: people search at the company domain (no email, no credit), then one people/match
reveal per chosen person (one credit each). Never more reveals than open slots.

Endpoints as documented in 2026: POST /api/v1/mixed_people/api_search and
POST /api/v1/people/match, API key in the `x-api-key` header. Personal emails and phone
numbers are never requested.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobengine import http
from jobengine.contacts.classify import classify_title
from jobengine.contacts.models import SLOT_TYPES, Candidate, ProviderResult
from jobengine.contacts.providers.base import ProviderDeps

NAME = "Apollo"
BASE = "https://api.apollo.io"
SEARCH = f"{BASE}/api/v1/mixed_people/api_search"
MATCH = f"{BASE}/api/v1/people/match"
PER_PAGE = 25
NOT_ON_PLAN = "apollo: not available on this plan"


def _headers(key: str) -> dict[str, str]:
    return {"x-api-key": key, "Content-Type": "application/json", "Cache-Control": "no-cache"}


def _titles(open_slots: Mapping[str, int], deps: ProviderDeps) -> list[str]:
    wanted = [key for key, kind in SLOT_TYPES.items() if open_slots.get(kind, 0) > 0]
    return [t for key in wanted for t in deps.search_titles.get(key, [])]


def search_body(domain: str, country: str, titles: list[str]) -> dict[str, Any]:
    return {
        "q_organization_domains_list": [domain],
        "person_titles": titles,
        "person_locations": [country],
        "page": 1,
        "per_page": PER_PAGE,
    }


def search(
    company: str, domain: str, country: str, open_slots: Mapping[str, int], deps: ProviderDeps,
) -> ProviderResult:
    key = deps.s.apollo_api_key
    if not key:
        return ProviderResult(skipped_reason="apollo: APOLLO_API_KEY not set")
    try:
        found = deps.request("POST", SEARCH, headers=_headers(key),
                             json_body=search_body(domain, country, _titles(open_slots, deps)))
    except http.HttpError as exc:
        if exc.status in (401, 403):
            return ProviderResult(skipped_reason=NOT_ON_PLAN)
        return ProviderResult(skipped_reason=f"apollo: {exc}")

    # Pick people per open slot from the (email-less) search results.
    remaining = dict(open_slots)
    picked: list[dict[str, Any]] = []
    for person in found.get("people") or []:
        kind = classify_title(person.get("title"), deps.patterns)
        if remaining.get(kind, 0) > 0:
            picked.append(person)
            remaining[kind] -= 1
    picked = picked[: max(0, min(sum(open_slots.values()), deps.credits_left))]

    result = ProviderResult()
    for person in picked:
        try:
            match = deps.request("POST", MATCH, headers=_headers(key),
                                 json_body={"id": person.get("id"),
                                            "reveal_personal_emails": False,
                                            "reveal_phone_number": False})
        except http.HttpError as exc:
            if exc.status in (401, 403):
                result.skipped_reason = NOT_ON_PLAN
                break
            result.notes.append(f"apollo: reveal failed: {exc}")
            continue
        result.credits_used += 1
        data = match.get("person") or {}
        email = data.get("email") or ""
        if not email:
            continue
        name = data.get("name") or " ".join(
            p for p in (data.get("first_name"), data.get("last_name")) if p)
        result.candidates.append(Candidate(
            name=name, first_name=data.get("first_name") or name.split(" ")[0],
            title=data.get("title") or person.get("title") or "", email=email,
            country=data.get("country") or person.get("country") or country,
            provider_verified=data.get("email_status") == "verified", source=NAME,
            person_id=person.get("id"),
        ))
    return result


def probe(domain: str, deps: ProviderDeps) -> tuple[int, list[str]]:
    """One search call, no reveal: (number of results, fields present)."""
    key = deps.s.apollo_api_key or ""
    found = deps.request("POST", SEARCH, headers=_headers(key),
                         json_body=search_body(domain, "", []))
    people = found.get("people") or []
    return len(people), sorted({k for p in people for k in p})
