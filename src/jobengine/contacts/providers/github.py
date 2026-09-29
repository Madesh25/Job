"""GitHub (PR 16): engineers of the company from its public GitHub organisation. Free.

1. The organisation: Config `contacts.github_orgs` (`Company Name = org-login` lines), else a
   GitHub search for organisations named like the company, accepted only when the
   organisation's own website or email is on the company's email domain (never a guess).
   The answer is kept in Bot State (`github_org:<company>`), so the search runs once.
2. Its public members (`GET /orgs/{org}/public_members`), then each profile
   (`GET /users/{login}`), at most `contacts.github.max_profiles` (20) profiles a job.
3. Only people whose own public profile shows an email on the company domain are kept; the
   email is exactly the one they published. Nobody is guessed.

Without GITHUB_TOKEN GitHub allows 60 calls an hour; with a token (no scopes needed) 5,000.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from jobengine import http
from jobengine.contacts.models import Candidate, ProviderResult
from jobengine.contacts.providers.base import ProviderDeps

NAME = "GitHub"
API = "https://api.github.com"
DEFAULT_PROFILES = 20
SEARCH_LIMIT = 5
COUNTRY_WORDS = {
    "Poland": ("poland", "polska", "warsaw", "warszawa", "krakow", "kraków", "wroclaw",
               "wrocław", "gdansk", "gdańsk", "poznan", "poznań", "lodz", "łódź", "katowice"),
    "Netherlands": ("netherlands", "nederland", "holland", "amsterdam", "rotterdam", "utrecht",
                    "eindhoven", "the hague", "den haag", "delft", "groningen"),
    "Ireland": ("ireland", "éire", "eire", "dublin", "cork", "galway", "limerick"),
}


def headers(token: str | None) -> dict[str, str]:
    out = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        out["Authorization"] = f"Bearer {token}"
    return out


def country_of(location: str | None) -> str | None:
    text = (location or "").casefold()
    for country, words in COUNTRY_WORDS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words):
            return country
    return None


def host_of(value: str | None) -> str:
    value = (value or "").strip().lower()
    value = value.removeprefix("https://").removeprefix("http://").removeprefix("www.")
    return value.split("/")[0].split("@")[-1]


def on_domain(host: str, domain: str) -> bool:
    return bool(host) and (host == domain or host.endswith("." + domain))


def find_org(company: str, domain: str, deps: ProviderDeps,
             known: Callable[[str], str | None]) -> tuple[str | None, int]:
    """(org login or None, calls made). `known` gives a configured or remembered login."""
    login = known(company)
    if login is not None:
        return (login or None), 0
    get = _getter(deps)
    calls = 1
    found = get(f"{API}/search/users", {"q": f"{company} type:org", "per_page": SEARCH_LIMIT})
    for item in (found or {}).get("items") or []:
        candidate = item.get("login") or ""
        if not candidate:
            continue
        calls += 1
        org = get(f"{API}/orgs/{quote(candidate)}", None) or {}
        if on_domain(host_of(org.get("blog")), domain) or \
                on_domain(host_of(org.get("email")), domain):
            return candidate, calls
    return None, calls


def _getter(deps: ProviderDeps) -> Callable[[str, dict[str, Any] | None], Any]:
    def get(url: str, params: dict[str, Any] | None) -> Any:
        return deps.request("GET", url, params=params, headers=headers(deps.s.github_token))

    return get


def search(company: str, domain: str, org: str, deps: ProviderDeps,
           max_profiles: int = DEFAULT_PROFILES) -> ProviderResult:
    """People of `org` with a public email on `domain`. Titles come from their bio."""
    get = _getter(deps)
    result = ProviderResult()
    try:
        members = get(f"{API}/orgs/{quote(org)}/public_members", {"per_page": 100}) or []
        result.credits_used = 1
        for member in members[:max(0, max_profiles)]:
            login = member.get("login") or ""
            if not login:
                continue
            profile = get(f"{API}/users/{quote(login)}", None) or {}
            result.credits_used += 1
            email = (profile.get("email") or "").strip()
            if not email or not on_domain(host_of(email), domain):
                continue
            name = (profile.get("name") or "").strip() or login
            where = country_of(profile.get("location"))
            bio = " ".join((profile.get("bio") or "").split())[:100]
            result.candidates.append(Candidate(
                name=name, first_name=name.split(" ")[0], title=bio, email=email,
                country=where, provider_verified=False, source=NAME,
                country_unverified=where is None, person_id=login))
    except http.HttpError as exc:
        if exc.status in (403, 429):
            result.skipped_reason = ("github: rate limit reached (set GITHUB_TOKEN for 5,000 "
                                     "calls an hour)")
        else:
            result.skipped_reason = f"github: {exc}"
    return result
