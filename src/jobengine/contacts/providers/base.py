"""Shared pieces for the providers: how they make HTTP calls, and the fixture transport used
outside prod (paid calls happen only in prod with DRY_RUN false)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jobengine import http
from jobengine.safety import assert_fetch_allowed
from jobengine.settings import ROOT_DIR, Settings

FIXTURES = ROOT_DIR / "fixtures" / "contacts"
# (method, url, params, headers, json body) -> decoded JSON
Request = Callable[..., Any]


def real_request(s: Settings) -> Request:
    def call(method: str, url: str, *, params: Any = None, headers: Mapping[str, str] | None = None,
             json_body: Any = None) -> Any:
        return http.request_json(method, url, params=params, headers=headers, json=json_body, s=s)

    return call


@dataclass
class FixtureHttp:
    """Answers provider requests from fixtures/contacts/ and records them. It still runs the
    host allowlist check, so a fake run proves the same rules as a real one."""

    s: Settings
    base: Path = FIXTURES
    calls: list[tuple[str, str]] = field(default_factory=list)
    routes: dict[str, str] = field(default_factory=lambda: dict(ROUTES))

    def __call__(self, method: str, url: str, *, params: Any = None,
                 headers: Mapping[str, str] | None = None, json_body: Any = None) -> Any:
        assert_fetch_allowed(url, self.s)
        parts = urlsplit(url)
        where = f"{parts.hostname}{parts.path}"
        self.calls.append((method, http.safe_url(url)))
        for prefix in sorted(self.routes, key=len, reverse=True):
            if where.startswith(prefix):
                data = json.loads((self.base / self.routes[prefix]).read_text(encoding="utf-8"))
                if isinstance(data, dict) and "by_key" in data:
                    # One answer per person id (JSON body) or per task hash (last path part).
                    key = (json_body or {}).get("id") or parts.path.rsplit("/", 1)[-1]
                    if key not in data["by_key"]:
                        raise http.HttpError(f"{method} {http.safe_url(url)} failed: no fixture "
                                             f"for {key}", 404)
                    return data["by_key"][key]
                return data
        raise http.HttpError(f"{method} {http.safe_url(url)} failed: no fixture", 404)


ROUTES = {
    "api.apollo.io/api/v1/mixed_people/api_search": "apollo_search.json",
    "api.apollo.io/api/v1/people/match": "apollo_match.json",
    "api.hunter.io/v2/domain-search": "hunter_domain_search.json",
    "api.snov.io/v1/oauth/access_token": "snov_token.json",
    "api.snov.io/v2/domain-search/prospects/start": "snov_prospects_start.json",
    "api.snov.io/v2/domain-search/prospects/result": "snov_prospects.json",
    "api.snov.io/v2/domain-search/prospects/search-emails/start": "snov_emails_start.json",
    "api.snov.io/v2/domain-search/prospects/search-emails/result": "snov_emails.json",
}


@dataclass
class ProviderDeps:
    s: Settings
    request: Request
    search_titles: Mapping[str, list[str]]  # slot key (peer, hiring, recruiter) -> titles
    credits_left: int = 10**6  # the provider's remaining monthly credits
    sleep: Callable[[float], None] = lambda seconds: None
    patterns: Mapping[str, str] | None = None
