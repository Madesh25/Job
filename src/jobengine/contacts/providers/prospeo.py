"""Prospeo: a people search at the company domain (one credit per page of up to 25, no
email), then one bulk enrich of the chosen people (one credit per email found). Never more
enrichments than open slots or credits left. Free plan: 75 credits a month, API included.

Endpoints as in Prospeo's official MCP server (@prospeo/prospeo-mcp-server): POST
https://api.prospeo.io/search-person and POST /bulk-enrich-person, API key in the `X-KEY`
header. Search results carry `person.person_id`, `current_job_title` and `location`;
enrichment answers `matched[].person.email` with `email` and `status` (VERIFIED or
UNVERIFIED). Only verified emails are asked for; mobiles never.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobengine import http
from jobengine.contacts.classify import classify_title
from jobengine.contacts.models import SLOT_TYPES, Candidate, ProviderResult
from jobengine.contacts.providers.base import ProviderDeps

NAME = "Prospeo"
BASE = "https://api.prospeo.io"
SEARCH = f"{BASE}/search-person"
ENRICH = f"{BASE}/bulk-enrich-person"
NOT_ON_PLAN = "prospeo: not available on this plan"


def _headers(key: str) -> dict[str, str]:
    return {"X-KEY": key, "Content-Type": "application/json"}


def _titles(open_slots: Mapping[str, int], deps: ProviderDeps) -> list[str]:
    wanted = [key for key, kind in SLOT_TYPES.items() if open_slots.get(kind, 0) > 0]
    return [t for key in wanted for t in deps.search_titles.get(key, [])]


def search_body(domain: str, titles: list[str]) -> dict[str, Any]:
    filters: dict[str, Any] = {"company": {"websites": {"include": [domain]}}}
    if titles:
        filters["person_job_title"] = {"include": titles, "match_mode": "CONTAINS"}
    return {"filters": filters, "page": 1}


def _error(answer: Mapping[str, Any]) -> str | None:
    return str(answer.get("error_code") or "error") if answer.get("error") else None


def search(
    company: str, domain: str, country: str, open_slots: Mapping[str, int], deps: ProviderDeps,
) -> ProviderResult:
    key = deps.s.prospeo_api_key
    if not key:
        return ProviderResult(skipped_reason="prospeo: PROSPEO_API_KEY not set")
    try:
        found = deps.request("POST", SEARCH, headers=_headers(key),
                             json_body=search_body(domain, _titles(open_slots, deps)))
    except http.HttpError as exc:
        if exc.status in (401, 403):
            return ProviderResult(skipped_reason=NOT_ON_PLAN)
        return ProviderResult(skipped_reason=f"prospeo: {exc}")
    code = _error(found)
    if code:
        # NO_RESULTS is a normal answer (nothing at this domain) and costs nothing.
        note = None if code == "NO_RESULTS" else f"prospeo: {code}"
        return ProviderResult(skipped_reason=note)
    result = ProviderResult(credits_used=1)

    # Pick people per open slot from the (email-less) search results.
    remaining = dict(open_slots)
    picked: list[dict[str, Any]] = []
    for row in found.get("results") or []:
        person = row.get("person") or {}
        kind = classify_title(person.get("current_job_title"), deps.patterns)
        if person.get("person_id") and remaining.get(kind, 0) > 0:
            picked.append(person)
            remaining[kind] -= 1
    room = max(0, min(sum(open_slots.values()), deps.credits_left - result.credits_used))
    picked = picked[:room]
    if not picked:
        return result

    body = {"data": [{"identifier": p["person_id"], "person_id": p["person_id"]}
                     for p in picked],
            "only_verified_email": True, "enrich_mobile": False}
    try:
        enriched = deps.request("POST", ENRICH, headers=_headers(key), json_body=body)
    except http.HttpError as exc:
        result.notes.append(f"prospeo: enrich failed: {exc}")
        return result
    code = _error(enriched)
    if code:
        result.notes.append(f"prospeo: enrich failed: {code}")
        return result
    matched = enriched.get("matched") or []
    cost = enriched.get("total_cost")
    result.credits_used += int(cost) if isinstance(cost, int | float) else len(matched)
    by_id = {p["person_id"]: p for p in picked}
    for match in matched:
        data = match.get("person") or {}
        mail = data.get("email") or {}
        email = mail.get("email") or ""
        if not email:
            continue
        searched = by_id.get(match.get("identifier") or data.get("person_id")) or {}
        name = data.get("full_name") or searched.get("full_name") or " ".join(
            p for p in (data.get("first_name"), data.get("last_name")) if p) or email
        where = (data.get("location") or searched.get("location") or {}).get("country")
        result.candidates.append(Candidate(
            name=name, first_name=data.get("first_name") or name.split(" ")[0],
            title=data.get("current_job_title") or searched.get("current_job_title") or "",
            email=email, country=where or None, provider_verified=mail.get("status") == "VERIFIED",
            source=NAME, country_unverified=not where, person_id=searched.get("person_id"),
        ))
    return result
