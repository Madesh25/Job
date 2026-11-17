"""Tomba: one domain search (limit 10), which costs one search credit. Free plan: 25 searches
a month, API included.

Endpoint as in Tomba's official SDKs (tomba-io/python, tomba-io/go): GET
https://api.tomba.io/v1/domain-search with the `X-Tomba-Key` and `X-Tomba-Secret` headers.
Each email has `email`, `first_name`, `last_name`, `position`, `country`, `type` (personal or
generic) and `verification.status`.
"""

from __future__ import annotations

from collections.abc import Mapping

from jobengine import http
from jobengine.contacts.models import Candidate, ProviderResult
from jobengine.contacts.providers.base import ProviderDeps

NAME = "Tomba"
DOMAIN_SEARCH = "https://api.tomba.io/v1/domain-search"
LIMIT = 10
# Tomba gives an ISO country code; the job's country is a name.
COUNTRY_CODES = {"PL": "Poland", "NL": "Netherlands", "IE": "Ireland"}


def headers(key: str, secret: str) -> dict[str, str]:
    return {"X-Tomba-Key": key, "X-Tomba-Secret": secret}


def country_name(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    return COUNTRY_CODES.get(value.upper(), value)


def search(
    company: str, domain: str, country: str, open_slots: Mapping[str, int], deps: ProviderDeps,
) -> ProviderResult:
    key, secret = deps.s.tomba_api_key, deps.s.tomba_api_secret
    if not key or not secret:
        return ProviderResult(skipped_reason="tomba: TOMBA_API_KEY or TOMBA_API_SECRET not set")
    try:
        found = deps.request("GET", DOMAIN_SEARCH, params={"domain": domain, "limit": LIMIT},
                             headers=headers(key, secret))
    except http.HttpError as exc:
        if exc.status in (401, 403):
            return ProviderResult(skipped_reason="tomba: not available on this plan")
        return ProviderResult(skipped_reason=f"tomba: {exc}")
    result = ProviderResult(credits_used=1)
    for item in (found.get("data") or {}).get("emails") or []:
        email = item.get("email") or ""
        if not email or item.get("type") == "generic":
            continue
        first, last = item.get("first_name") or "", item.get("last_name") or ""
        name = item.get("full_name") or " ".join(p for p in (first, last) if p) or email
        where = country_name(item.get("country"))
        status = (item.get("verification") or {}).get("status")
        result.candidates.append(Candidate(
            name=name, first_name=first or name.split(" ")[0], title=item.get("position") or "",
            email=email, country=where, provider_verified=status == "valid", source=NAME,
            country_unverified=where is None,
        ))
    return result
