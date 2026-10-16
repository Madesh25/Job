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
from jobengine.contacts.classify import classify_title, wide_kind
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
MAX_POSITIONS = 10  # Snov refuses more (16 Oct: 11 titles gave HTTP 422)


def token(deps: ProviderDeps) -> str:
    data = deps.request("POST", TOKEN, json_body={
        "grant_type": "client_credentials",
        "client_id": deps.s.snov_client_id,
        "client_secret": deps.s.snov_client_secret,
    })
    return data.get("access_token") or ""


def _task_hash(data: dict[str, Any]) -> str:
    return (data.get("meta") or {}).get("task_hash") or data.get("task_hash") or ""


def _result_url(started: dict[str, Any], base: str) -> str:
    """Where a started task's result is: Snov's own `links.result` when it gives one."""
    link = (started.get("links") or {}).get("result")
    if isinstance(link, str) and link.startswith(BASE + "/"):
        return link
    return f"{base}/{_task_hash(started)}"


def email_start_url(prospect: dict[str, Any]) -> str | None:
    """The email search for one prospect: Snov's own `search_emails_start` link, else the
    start URL with the prospect's hash. None when the answer has neither (16 Oct: the live
    answer had no `prospect_hash`, so the bot called .../search-emails/start/ and got 404)."""
    link = prospect.get("search_emails_start")
    if isinstance(link, str) and link.startswith(f"{EMAILS_START}/"):
        return link
    prospect_hash = prospect.get("prospect_hash") or prospect.get("hash")
    return f"{EMAILS_START}/{prospect_hash}" if prospect_hash else None


def _poll(deps: ProviderDeps, url: str, auth: dict[str, str]) -> dict[str, Any]:
    for attempt in range(POLLS):
        data = deps.request("GET", url, headers=auth)
        if data.get("status") in (None, "completed"):
            return data
        deps.sleep(POLL_SECONDS * (attempt + 1))
    return {}


def _positions(open_slots: Mapping[str, int], deps: ProviderDeps) -> list[str]:
    wanted = [key for key, kind in SLOT_TYPES.items() if open_slots.get(kind, 0) > 0]
    return [t for key in wanted for t in deps.search_titles.get(key, [])][:MAX_POSITIONS]


def prospects_params(domain: str, positions: list[str]) -> list[tuple[str, str | int]]:
    """domain, page and positions[] as query parameters, the form Snov documents."""
    return [("domain", domain), ("page", 1), *(("positions[]", p) for p in positions)]


def search(
    company: str, domain: str, country: str, open_slots: Mapping[str, int], deps: ProviderDeps,
) -> ProviderResult:
    if not (deps.s.snov_client_id and deps.s.snov_client_secret):
        return ProviderResult(skipped_reason="snov: SNOV_CLIENT_ID or SNOV_CLIENT_SECRET not set")
    try:
        auth = {"Authorization": f"Bearer {token(deps)}"}
        start = deps.request("POST", PROSPECTS_START, headers=auth,
                             params=prospects_params(domain, _positions(open_slots, deps)))
        prospects = _poll(deps, _result_url(start, PROSPECTS_RESULT), auth)
    except http.HttpError as exc:
        if exc.status in (401, 403):
            return ProviderResult(skipped_reason="snov: not available on this plan")
        return ProviderResult(skipped_reason=f"snov: {exc}")

    remaining = dict(open_slots)
    picked = []
    for prospect in prospects.get("data") or []:
        kind = classify_title(prospect.get("position"), deps.patterns)
        if kind not in remaining:
            kind = wide_kind(prospect.get("position"))
        if remaining.get(kind, 0) > 0:
            picked.append(prospect)
            remaining[kind] -= 1
    picked = picked[: max(0, min(sum(open_slots.values()), deps.credits_left))]

    result = ProviderResult()
    people = prospects.get("data") or []
    if not picked:
        # 16 Oct: Snov said nothing at all; now it says what it saw.
        result.notes.append(f"snov: {len(people)} people at {domain}, none fit the open slots"
                            if people else f"snov: no people found at {domain}")
    for prospect in picked:
        start_url = email_start_url(prospect)
        if start_url is None:
            result.notes.append("snov: no email-search link in Snov's answer (fields: "
                                + ", ".join(sorted(prospect)) + ")")
            continue
        try:
            started = deps.request("POST", start_url, headers=auth)
            found = _poll(deps, _result_url(started, EMAILS_RESULT), auth)
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
                         params=prospects_params(domain, []))
    prospects = (_poll(deps, _result_url(start, PROSPECTS_RESULT), auth).get("data")) or []
    return len(prospects), sorted({k for p in prospects for k in p})
