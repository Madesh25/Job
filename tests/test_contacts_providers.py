import logging

import httpx
import pytest

from jobengine import http
from jobengine.contacts.models import HIRING, PEER, RECRUITER
from jobengine.contacts.providers import apollo, hunter, snov
from jobengine.contacts.providers.base import FixtureHttp, ProviderDeps, real_request
from jobengine.settings import load_settings

KEYS = {"APOLLO_API_KEY": "apollo-test-key", "HUNTER_API_KEY": "hunter-secret-key-123",
        "SNOV_CLIENT_ID": "snov-id", "SNOV_CLIENT_SECRET": "snov-secret"}
TITLES = {"peer": ["DevOps Engineer"], "hiring": ["Engineering Manager"],
          "recruiter": ["Recruiter"]}
DOMAIN = "vistula.example.com"


def deps(**environ):
    s = load_settings("local", {**KEYS, **environ})
    fake = FixtureHttp(s)
    return ProviderDeps(s=s, request=fake, search_titles=TITLES), fake


def test_apollo_reveals_exactly_the_open_slots():
    d, fake = deps()
    result = apollo.search("Vistula Cloud", DOMAIN, "Poland", {PEER: 1, HIRING: 1, RECRUITER: 0}, d)
    reveals = [c for c in fake.calls if c[1].endswith("/people/match")]
    assert len(reveals) == 2 and result.credits_used == 2
    assert [(c.name, c.provider_verified, c.source) for c in result.candidates] == [
        ("Anna Example", True, "Apollo"), ("Piotr Example", True, "Apollo")]
    assert fake.calls[0] == ("POST", "https://api.apollo.io/api/v1/mixed_people/api_search")


def test_apollo_never_reveals_more_than_the_credits_left():
    d, fake = deps()
    d.credits_left = 1
    result = apollo.search("Vistula Cloud", DOMAIN, "Poland", {PEER: 2, HIRING: 1}, d)
    assert result.credits_used == 1
    assert len([c for c in fake.calls if c[1].endswith("/people/match")]) == 1


def test_apollo_search_body_filters_domain_titles_and_country():
    body = apollo.search_body(DOMAIN, "Poland", ["DevOps Engineer"])
    assert body["q_organization_domains_list"] == [DOMAIN]
    assert body["person_locations"] == ["Poland"] and body["person_titles"] == ["DevOps Engineer"]


def test_missing_keys_skip_without_calls():
    s = load_settings("local", {})
    fake = FixtureHttp(s)
    d = ProviderDeps(s=s, request=fake, search_titles=TITLES)
    for provider, reason in ((apollo, "APOLLO_API_KEY"), (hunter, "HUNTER_API_KEY"),
                             (snov, "SNOV_CLIENT_ID")):
        assert reason in provider.search("X", DOMAIN, "Poland", {PEER: 1}, d).skipped_reason
    assert fake.calls == []


def test_hunter_one_call_and_verification():
    d, fake = deps()
    result = hunter.search("Vistula Cloud", DOMAIN, "Poland", {PEER: 1}, d)
    assert len(fake.calls) == 1 and result.credits_used == 1
    emails = {c.email: c for c in result.candidates}
    assert "info@vistula.example.com" not in emails  # generic mailbox
    assert emails["jan.example@vistula.example.com"].provider_verified is True
    assert emails["hanna.example@vistula.example.com"].provider_verified is False
    assert all(c.country_unverified for c in result.candidates)


def test_snov_token_prospects_then_emails_for_chosen_only():
    d, fake = deps()
    result = snov.search("Vistula Cloud", DOMAIN, "Poland", {RECRUITER: 1}, d)
    paths = [c[1].split("snov.io")[1] for c in fake.calls]
    assert paths[0] == "/v1/oauth/access_token"
    assert paths[1] == "/v2/domain-search/prospects/start"
    assert paths[2] == "/v2/domain-search/prospects/result/sn-prospects-1"
    assert paths[3:] == ["/v2/domain-search/prospects/search-emails/start/sn-rita",
                         "/v2/domain-search/prospects/search-emails/result/sn-em-rita"]
    assert [(c.email, c.provider_verified) for c in result.candidates] == [
        ("rita.example@vistula.example.com", True)]
    assert result.credits_used == 1


def test_plan_errors_skip_the_provider():
    s = load_settings("local", KEYS)

    def forbidden(method, url, **kw):
        raise http.HttpError(f"{method} {http.safe_url(url)} failed: HTTP 403", 403)

    d = ProviderDeps(s=s, request=forbidden, search_titles=TITLES)
    assert apollo.search("X", DOMAIN, "Poland", {PEER: 1}, d).skipped_reason == (
        "apollo: not available on this plan")
    assert hunter.search("X", DOMAIN, "Poland", {PEER: 1}, d).skipped_reason == (
        "hunter: not available on this plan")


def test_hunter_key_never_in_logs_or_errors(caplog):
    s = load_settings("prod", {**KEYS, "DRY_RUN": "false"})
    http.set_transport(httpx.MockTransport(lambda request: httpx.Response(500, json={})))
    try:
        d = ProviderDeps(s=s, request=real_request(s), search_titles=TITLES)
        with caplog.at_level(logging.DEBUG):
            result = hunter.search("Vistula Cloud", DOMAIN, "Poland", {PEER: 1}, d)
    finally:
        http.set_transport(None)
    assert "HTTP 500" in result.skipped_reason
    assert "hunter-secret-key-123" not in result.skipped_reason
    assert "hunter-secret-key-123" not in caplog.text


def test_fixture_http_enforces_the_host_allowlist():
    from jobengine.safety import SafetyError

    d, fake = deps()
    with pytest.raises(SafetyError):
        fake("GET", "https://www.linkedin.com/in/someone")


def test_probes_make_one_search_call():
    for provider, expected in ((apollo, 4), (hunter, 6), (snov, 3)):
        d, fake = deps()
        count, fields = provider.probe(DOMAIN, d)
        assert count == expected and fields
        searches = [c for c in fake.calls if "match" not in c[1] and "search-emails" not in c[1]]
        assert len(searches) == len(fake.calls)


def test_probe_is_refused_outside_prod_or_in_dry_run():
    from jobengine.contacts.probe import REFUSED, run_probe

    for env, environ in (("local", {}), ("dev", {}), ("prod", {"DRY_RUN": "true"})):
        with pytest.raises(PermissionError, match="only runs with APP_ENV=prod"):
            run_probe("hunter", DOMAIN, load_settings(env, {**KEYS, **environ}))
    assert "DRY_RUN=false" in REFUSED
