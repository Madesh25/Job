"""Hunter: one domain search (type personal, IT/management/HR departments, limit 10), which
normally costs one credit. The API key is a query parameter, so it must never be logged:
jobengine.http only ever logs URLs without their query string.

Endpoint: GET https://api.hunter.io/v2/domain-search. Hunter has no per-person location, so
its candidates are marked country_unverified.
"""

from __future__ import annotations

from collections.abc import Mapping

from jobengine import http
from jobengine.contacts.models import Candidate, ProviderResult
from jobengine.contacts.providers.base import ProviderDeps

NAME = "Hunter"
DOMAIN_SEARCH = "https://api.hunter.io/v2/domain-search"
DEPARTMENTS = "it,management,hr"
LIMIT = 10


def params(domain: str, key: str) -> dict[str, str | int]:
    return {"domain": domain, "type": "personal", "department": DEPARTMENTS, "limit": LIMIT,
            "api_key": key}


def search(
    company: str, domain: str, country: str, open_slots: Mapping[str, int], deps: ProviderDeps,
) -> ProviderResult:
    key = deps.s.hunter_api_key
    if not key:
        return ProviderResult(skipped_reason="hunter: HUNTER_API_KEY not set")
    try:
        found = deps.request("GET", DOMAIN_SEARCH, params=params(domain, key))
    except http.HttpError as exc:
        if exc.status in (401, 403):
            return ProviderResult(skipped_reason="hunter: not available on this plan")
        return ProviderResult(skipped_reason=f"hunter: {exc}")
    data = found.get("data") or {}
    result = ProviderResult(credits_used=1)
    for item in data.get("emails") or []:
        email = item.get("value") or ""
        if not email or item.get("type") == "generic":
            continue
        first, last = item.get("first_name") or "", item.get("last_name") or ""
        name = " ".join(p for p in (first, last) if p) or email
        verification = (item.get("verification") or {}).get("status")
        result.candidates.append(Candidate(
            name=name, first_name=first or name.split(" ")[0], title=item.get("position") or "",
            email=email, country=None, provider_verified=verification == "valid",
            source=NAME, country_unverified=True,
        ))
    return result


def probe(domain: str, deps: ProviderDeps) -> tuple[int, list[str]]:
    found = deps.request("GET", DOMAIN_SEARCH, params=params(domain, deps.s.hunter_api_key or ""))
    emails = (found.get("data") or {}).get("emails") or []
    return len(emails), sorted({k for e in emails for k in e})
