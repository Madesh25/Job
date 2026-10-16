"""Prospeo: a people search at the company domain (one credit per page of up to 25, no
email), then one enrich call per chosen person (one credit per email found). Never more
enrichments than open slots or credits left. Free plan: 75 credits a month, API included,
about one call a second (16 Oct: the bulk call was refused with HTTP 429).

Endpoints as in Prospeo's official MCP server (@prospeo/prospeo-mcp-server): POST
https://api.prospeo.io/search-person and POST /enrich-person, API key in the `X-KEY`
header. Search results carry `person.person_id`, `current_job_title` and `location`;
enrichment answers `person.email` with `email` and `status` (VERIFIED or UNVERIFIED), or
NO_MATCH (no credit). Only verified emails are asked for; mobiles never.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobengine import http
from jobengine.contacts.classify import classify_title, wide_kind
from jobengine.contacts.models import SLOT_TYPES, Candidate, ProviderResult
from jobengine.contacts.providers.base import ProviderDeps

NAME = "Prospeo"
BASE = "https://api.prospeo.io"
SEARCH = f"{BASE}/search-person"
ENRICH = f"{BASE}/enrich-person"  # one person per call (free plan)
NOT_ON_PLAN = "prospeo: not available on this plan"
# Wider search (16 Oct): when the titles leave slots open, one more search for senior people
# in the company's engineering and IT departments (values from Prospeo's filter list).
WIDE_SENIORITY = ["Senior", "Manager", "Head", "Director", "Vice President"]
WIDE_DEPARTMENTS = ["Engineering & Technical", "Information Technology"]
WIDE_DEPARTMENT = "engineering"  # what wide_kind gets for these people
ENRICH_PAUSE = 1.5  # seconds before each call after the first (free plan: ~1 a second)


def _headers(key: str) -> dict[str, str]:
    return {"X-KEY": key, "Content-Type": "application/json"}


def _titles(open_slots: Mapping[str, int], deps: ProviderDeps) -> list[str]:
    wanted = [key for key, kind in SLOT_TYPES.items() if open_slots.get(kind, 0) > 0]
    return [t for key in wanted for t in deps.search_titles.get(key, [])]


LOCATIONS = ("Poland", "Netherlands", "Ireland")  # names as Prospeo's location list has them


def _located(body: dict[str, Any], country: str) -> dict[str, Any]:
    """16 Oct (strict location rule): search only people in the job's country."""
    if country in LOCATIONS:
        body["filters"]["person_location_search"] = {"include": [country]}
    return body


def search_body(domain: str, titles: list[str], country: str = "") -> dict[str, Any]:
    filters: dict[str, Any] = {"company": {"websites": {"include": [domain]}}}
    if titles:
        filters["person_job_title"] = {"include": titles, "match_mode": "CONTAINS"}
    return _located({"filters": filters, "page": 1}, country)


def wide_body(domain: str, country: str = "") -> dict[str, Any]:
    return _located({"filters": {"company": {"websites": {"include": [domain]}},
                                 "person_seniority": {"include": WIDE_SENIORITY},
                                 "person_department": {"include": WIDE_DEPARTMENTS}},
                     "page": 1}, country)


HR_DEPARTMENTS = ["Human Resources"]


def _rounds(domain: str, country: str) -> list[tuple[str, dict[str, Any], str | None,
                                                      tuple[str, ...]]]:
    """(what, body, department for wide_kind, slot keys it can fill), in order."""
    hr = {"filters": {"company": {"websites": {"include": [domain]}},
                      "person_department": {"include": HR_DEPARTMENTS}}, "page": 1}
    anyone = {"filters": {"company": {"websites": {"include": [domain]}}}, "page": 1}
    return [("wider search", wide_body(domain, country), WIDE_DEPARTMENT, ("peer", "hiring")),
            ("HR search", _located(hr, country), "hr", ("recruiter",)),
            ("whole company", _located(anyone, country), None, ("peer", "hiring", "recruiter"))]


def _search(key: str, body: dict[str, Any], deps: ProviderDeps,
            notes: list[str] | None = None) -> tuple[list[Any], str | None]:
    """(results, problem). NO_RESULTS is a normal answer (HTTP 400 from Prospeo, no credit);
    a problem is any other refusal. A refused country filter is tried once without it (the
    people are still checked by their location afterwards)."""
    rows, problem = _search_once(key, body, deps)
    filters = body.get("filters") or {}
    if problem and "INVALID_FILTERS" in problem and "person_location_search" in filters:
        if notes is not None:
            notes.append("prospeo: its country filter was refused, searched without it")
        rest = {k: v for k, v in filters.items() if k != "person_location_search"}
        return _search_once(key, {**body, "filters": rest}, deps)
    return rows, problem


def _search_once(key: str, body: dict[str, Any],
                 deps: ProviderDeps) -> tuple[list[Any], str | None]:
    try:
        found = deps.request("POST", SEARCH, headers=_headers(key), json_body=body)
    except http.HttpError as exc:
        if exc.status in (401, 403):
            return [], NOT_ON_PLAN
        if "NO_RESULTS" in str(exc):
            return [], None
        return [], f"prospeo: {exc}"
    code = _error(found)
    if code:
        return [], None if code == "NO_RESULTS" else f"prospeo: {code}"
    return list(found.get("results") or []), None


def locate(email: str, deps: ProviderDeps) -> tuple[str | None, int]:
    """(country, credits) for a person whose email another site found (16 Oct: Hunter gives
    no location, so the strict rule left all its people out). One /enrich-person call by
    email; a match costs one credit, NO_MATCH none."""
    key = deps.s.prospeo_api_key
    if not key:
        return None, 0
    body = {"data": {"email": email}, "only_verified_email": False, "enrich_mobile": False}
    try:
        found = deps.request("POST", ENRICH, headers=_headers(key), json_body=body)
    except http.HttpError:
        return None, 0
    person = found.get("person") if not _error(found) else None
    if not person:
        return None, 0
    return (person.get("location") or {}).get("country") or None, 1


def _error(answer: Mapping[str, Any]) -> str | None:
    return str(answer.get("error_code") or "error") if answer.get("error") else None


def search(
    company: str, domain: str, country: str, open_slots: Mapping[str, int], deps: ProviderDeps,
) -> ProviderResult:
    key = deps.s.prospeo_api_key
    if not key:
        return ProviderResult(skipped_reason="prospeo: PROSPEO_API_KEY not set")
    notes: list[str] = []
    rows, problem = _search(key, search_body(domain, _titles(open_slots, deps), country), deps,
                            notes)
    if problem:
        return ProviderResult(skipped_reason=problem, notes=notes)
    result = ProviderResult(credits_used=1 if rows else 0, notes=notes)

    # Pick people per open slot from the (email-less) search results.
    remaining = dict(open_slots)
    picked: list[dict[str, Any]] = []
    for row in rows:
        person = row.get("person") or {}
        kind = classify_title(person.get("current_job_title"), deps.patterns)
        if person.get("person_id") and remaining.get(kind, 0) > 0:
            picked.append(person)
            remaining[kind] -= 1
    # More searches while slots stay open (16 Oct: one senior search found 1 of 4 at HCLTech
    # in the Netherlands): senior engineering/IT people, then HR people for the recruiter,
    # then anyone at the company in the country. One credit each when it finds people.
    wide_ids: set[str] = set()
    known = {p["person_id"] for p in picked}
    for what, body, department, kinds in _rounds(domain, country):
        if not any(remaining.get(SLOT_TYPES[k], 0) > 0 for k in kinds):
            continue
        if deps.credits_left - result.credits_used <= 1:
            break
        deps.sleep(ENRICH_PAUSE)
        rows, problem = _search(key, body, deps, result.notes)
        if problem:
            result.notes.append(f"{problem} ({what})")
        result.credits_used += 1 if rows else 0
        for row in rows:
            person = row.get("person") or {}
            title = person.get("current_job_title")
            kind = classify_title(title, deps.patterns)
            if kind not in remaining:
                kind = wide_kind(title, department)
            pid = person.get("person_id")
            if pid and pid not in known and remaining.get(kind, 0) > 0:
                picked.append(person)
                known.add(pid)
                wide_ids.add(pid)
                remaining[kind] -= 1
    room = max(0, min(sum(open_slots.values()), deps.credits_left - result.credits_used))
    picked = picked[:room]
    if not picked:
        return result

    # 16 Oct: the bulk email call got HTTP 429 even after 22 seconds; Prospeo's free plan
    # takes one person per call, about one call a second. So: one /enrich-person per person.
    for person in picked:
        deps.sleep(ENRICH_PAUSE)
        body = {"data": {"person_id": person["person_id"]},
                "only_verified_email": True, "enrich_mobile": False}
        try:
            enriched = deps.request("POST", ENRICH, headers=_headers(key), json_body=body)
        except http.HttpError as exc:
            if "NO_MATCH" not in str(exc):  # no verified email for this person: no credit
                result.notes.append(f"prospeo: enrich failed: {exc}")
            continue
        code = _error(enriched)
        if code:
            if code != "NO_MATCH":
                result.notes.append(f"prospeo: enrich failed: {code}")
            continue
        data = enriched.get("person") or {}
        mail = data.get("email") or {}
        email = mail.get("email") or ""
        if not email:
            continue
        result.credits_used += 1
        name = data.get("full_name") or person.get("full_name") or " ".join(
            p for p in (data.get("first_name"), data.get("last_name")) if p) or email
        where = (data.get("location") or person.get("location") or {}).get("country")
        result.candidates.append(Candidate(
            name=name, first_name=data.get("first_name") or name.split(" ")[0],
            title=data.get("current_job_title") or person.get("current_job_title") or "",
            email=email, country=where or None, provider_verified=mail.get("status") == "VERIFIED",
            source=NAME, country_unverified=not where, person_id=person.get("person_id"),
            department=WIDE_DEPARTMENT if person.get("person_id") in wide_ids else None,
        ))
    return result
