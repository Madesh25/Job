"""Snov: OAuth client credentials, then a domain search for prospects (positions filter), then
an email search for the chosen prospects only (one credit per prospect searched).

Endpoints (Snov API v2, asynchronous start/result pairs):
POST /v1/oauth/access_token, POST /v2/domain-search/prospects/start,
GET /v2/domain-search/prospects/result/<task_hash>,
POST /v2/domain-search/prospects/search-emails/start/<prospect_hash>,
GET /v2/domain-search/prospects/search-emails/result/<task_hash>.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobengine import http
from jobengine.contacts.classify import classify_title
from jobengine.contacts.models import SLOT_TYPES, Candidate, ProviderResult
from jobengine.contacts.providers.base import ProviderDeps

NAME = "Snov"
BASE = "https://api.snov.io"
TOKEN = f"{BASE}/v1/oauth/access_token"
PROSPECTS_START = f"{BASE}/v2/domain-search/prospects/start"
PROSPECTS_RESULT = f"{BASE}/v2/domain-search/prospects/result"
EMAILS_START = f"{BASE}/v2/domain-search/prospects/search-emails/start"
EMAILS_RESULT = f"{BASE}/v2/domain-search/prospects/search-emails/result"
POLLS = 5
POLL_SECONDS = 2.0


def token(deps: ProviderDeps) -> str:
    data = deps.request("POST", TOKEN, json_body={
        "grant_type": "client_credentials",
        "client_id": deps.s.snov_client_id,
        "client_secret": deps.s.snov_client_secret,
    })
    return data.get("access_token") or ""


def _task_hash(data: dict[str, Any]) -> str:
    return (data.get("meta") or {}).get("task_hash") or data.get("task_hash") or ""


def _poll(deps: ProviderDeps, url: str, auth: dict[str, str]) -> dict[str, Any]:
    for attempt in range(POLLS):
        data = deps.request("GET", url, headers=auth)
        if data.get("status") in (None, "completed"):
            return data
        deps.sleep(POLL_SECONDS * (attempt + 1))
    return {}


def _positions(open_slots: Mapping[str, int], deps: ProviderDeps) -> list[str]:
    wanted = [key for key, kind in SLOT_TYPES.items() if open_slots.get(kind, 0) > 0]
    return [t for key in wanted for t in deps.search_titles.get(key, [])]


def search(
    company: str, domain: str, country: str, open_slots: Mapping[str, int], deps: ProviderDeps,
) -> ProviderResult:
    if not (deps.s.snov_client_id and deps.s.snov_client_secret):
        return ProviderResult(skipped_reason="snov: SNOV_CLIENT_ID or SNOV_CLIENT_SECRET not set")
    try:
        auth = {"Authorization": f"Bearer {token(deps)}"}
        start = deps.request("POST", PROSPECTS_START, headers=auth,
                             json_body={"domain": domain, "page": 1,
                                        "positions": _positions(open_slots, deps)})
        prospects = _poll(deps, f"{PROSPECTS_RESULT}/{_task_hash(start)}", auth)
    except http.HttpError as exc:
        if exc.status in (401, 403):
            return ProviderResult(skipped_reason="snov: not available on this plan")
        return ProviderResult(skipped_reason=f"snov: {exc}")

    remaining = dict(open_slots)
    picked = []
    for prospect in prospects.get("data") or []:
        kind = classify_title(prospect.get("position"), deps.patterns)
        if remaining.get(kind, 0) > 0:
            picked.append(prospect)
            remaining[kind] -= 1
    picked = picked[: max(0, min(sum(open_slots.values()), deps.credits_left))]

    result = ProviderResult()
    for prospect in picked:
        prospect_hash = prospect.get("prospect_hash") or prospect.get("hash") or ""
        try:
            started = deps.request("POST", f"{EMAILS_START}/{prospect_hash}", headers=auth)
            found = _poll(deps, f"{EMAILS_RESULT}/{_task_hash(started)}", auth)
        except http.HttpError as exc:
            result.notes.append(f"snov: email search failed: {exc}")
            continue
        result.credits_used += 1
        emails = ((found.get("data") or {}).get("emails")) or []
        best = next((e for e in emails if e.get("smtp_status") == "valid"),
                    emails[0] if emails else None)
        if not best or not best.get("email"):
            continue
        first, last = prospect.get("first_name") or "", prospect.get("last_name") or ""
        name = " ".join(p for p in (first, last) if p)
        result.candidates.append(Candidate(
            name=name or best["email"], first_name=first, title=prospect.get("position") or "",
            email=best["email"], country=prospect.get("country") or None,
            provider_verified=best.get("smtp_status") == "valid", source=NAME,
            country_unverified=not prospect.get("country"),
        ))
    return result


def probe(domain: str, deps: ProviderDeps) -> tuple[int, list[str]]:
    auth = {"Authorization": f"Bearer {token(deps)}"}
    start = deps.request("POST", PROSPECTS_START, headers=auth,
                         json_body={"domain": domain, "page": 1})
    prospects = (_poll(deps, f"{PROSPECTS_RESULT}/{_task_hash(start)}", auth).get("data")) or []
    return len(prospects), sorted({k for p in prospects for k in p})
