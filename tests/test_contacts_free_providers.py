"""PR 10: Prospeo and Tomba free plans in the contact search."""

from datetime import date

from jobengine import http
from jobengine.config_store import ConfigRow, ConfigStore
from jobengine.contacts.credits import CreditBook
from jobengine.contacts.finder import fake_deps, find_contacts
from jobengine.contacts.providers import prospeo, tomba
from jobengine.contacts.providers.base import FixtureHttp, ProviderDeps
from jobengine.safety import check_config_write
from jobengine.settings import load_settings

TODAY = date(2026, 10, 1)
PL = "fixture-clean-pl"
DOMAIN = "vistula.example.com"
KEYS = {"PROSPEO_API_KEY": "p-key", "TOMBA_API_KEY": "t-key", "TOMBA_API_SECRET": "t-secret"}


def settings(**environ):
    return load_settings("local", {**KEYS, **environ})


def pdeps(s=None, credits_left=10**6, request=None):
    s = s or settings()
    fixture = request or FixtureHttp(s)
    return ProviderDeps(s=s, request=fixture, credits_left=credits_left,
                        search_titles={"peer": ["DevOps Engineer", "Platform Engineer"],
                                       "hiring": ["Engineering Manager"],
                                       "recruiter": ["Recruiter"]},
                        patterns=s.contacts.get("title_patterns")), fixture


# ---------------------------------------------------------------- Tomba


def test_tomba_domain_search():
    deps, fixture = pdeps()
    result = tomba.search("Vistula Cloud", DOMAIN, "Poland", {"Peer engineer": 1}, deps)
    assert fixture.calls == [("GET", "https://api.tomba.io/v1/domain-search")]
    assert result.credits_used == 1
    marta, tomasz = result.candidates  # the generic jobs@ mailbox is left out
    assert (marta.name, marta.email, marta.title) == (
        "Marta Example", "marta.example@vistula.example.com", "Site Reliability Engineer")
    assert (marta.country, marta.country_unverified, marta.provider_verified) == (
        "Poland", False, True)  # "PL" is read as Poland
    assert (tomasz.country, tomasz.country_unverified, tomasz.provider_verified) == (
        None, True, False)  # no country, accept_all is not verified
    assert marta.source == "Tomba"


def test_tomba_sends_both_keys_and_needs_both():
    seen = {}

    def request(method, url, *, params=None, headers=None, json_body=None):
        seen.update(headers=headers, params=params)
        return {"data": {"emails": []}}

    deps, _ = pdeps(request=request)
    tomba.search("Vistula Cloud", DOMAIN, "Poland", {"Peer engineer": 1}, deps)
    assert seen == {"headers": {"X-Tomba-Key": "t-key", "X-Tomba-Secret": "t-secret"},
                    "params": {"domain": DOMAIN, "limit": 10}}
    deps, _ = pdeps(s=load_settings("local", {"TOMBA_API_KEY": "t-key"}))
    assert tomba.search("V", DOMAIN, "Poland", {}, deps).skipped_reason == (
        "tomba: TOMBA_API_KEY or TOMBA_API_SECRET not set")


def test_tomba_refused_key_is_skipped():
    def refused(*args, **kwargs):
        raise http.HttpError("GET https://api.tomba.io/v1/domain-search failed: 401", 401)

    deps, _ = pdeps(request=refused)
    assert tomba.search("V", DOMAIN, "Poland", {}, deps).skipped_reason == (
        "tomba: not available on this plan")


# ---------------------------------------------------------------- Prospeo


def test_prospeo_search_then_reveal_only_open_slots():
    bodies = []
    fixture = FixtureHttp(settings())

    def request(method, url, **kwargs):
        bodies.append((url, kwargs.get("json_body"), kwargs.get("headers")))
        return fixture(method, url, **kwargs)

    deps, _ = pdeps(request=request)
    result = prospeo.search("Vistula Cloud", DOMAIN, "Poland",
                            {"Peer engineer": 1, "Hiring": 1}, deps)
    (search_url, search_body, headers), (enrich_url, enrich_body, _) = bodies
    assert search_url == "https://api.prospeo.io/search-person"
    assert headers["X-KEY"] == "p-key"
    assert search_body["filters"]["company"] == {"websites": {"include": [DOMAIN]}}
    assert search_body["filters"]["person_job_title"]["match_mode"] == "CONTAINS"
    assert "Engineering Manager" in search_body["filters"]["person_job_title"]["include"]
    assert enrich_url == "https://api.prospeo.io/bulk-enrich-person"
    # The office manager fills no slot, so only two people are revealed; verified only.
    assert enrich_body == {"data": [{"identifier": "prs-1", "person_id": "prs-1"},
                                    {"identifier": "prs-2", "person_id": "prs-2"}],
                           "only_verified_email": True, "enrich_mobile": False}
    assert result.credits_used == 3  # 1 search page + total_cost 2
    assert [(c.name, c.email, c.country, c.provider_verified, c.source)
            for c in result.candidates] == [
        ("Ewa Example", "ewa.example@vistula.example.com", "Poland", True, "Prospeo"),
        ("Piotr Sample", "piotr.sample@vistula.example.com", "Poland", True, "Prospeo")]


def test_prospeo_never_reveals_more_than_the_credits_left():
    bodies = []
    fixture = FixtureHttp(settings())

    def request(method, url, **kwargs):
        bodies.append(kwargs.get("json_body"))
        return fixture(method, url, **kwargs)

    deps, _ = pdeps(request=request, credits_left=2)  # 1 for the search, 1 reveal
    prospeo.search("V", DOMAIN, "Poland", {"Peer engineer": 1, "Hiring": 1}, deps)
    assert len(bodies[1]["data"]) == 1
    deps, _ = pdeps(credits_left=1)  # only the search fits: no reveal
    result = prospeo.search("V", DOMAIN, "Poland", {"Peer engineer": 1}, deps)
    assert result.credits_used == 1 and result.candidates == []


def test_prospeo_no_results_and_errors():
    def answer(value):
        return lambda *args, **kwargs: value

    deps, _ = pdeps(request=answer({"error": True, "error_code": "NO_RESULTS"}))
    result = prospeo.search("V", DOMAIN, "Poland", {"Peer engineer": 1}, deps)
    assert (result.skipped_reason, result.credits_used, result.candidates) == (None, 0, [])
    deps, _ = pdeps(request=answer({"error": True, "error_code": "INSUFFICIENT_CREDITS"}))
    assert prospeo.search("V", DOMAIN, "Poland", {}, deps).skipped_reason == (
        "prospeo: INSUFFICIENT_CREDITS")
    deps, _ = pdeps(s=load_settings("local", {}))
    assert prospeo.search("V", DOMAIN, "Poland", {}, deps).skipped_reason == (
        "prospeo: PROSPEO_API_KEY not set")


# ---------------------------------------------------------------- the waterfall


def config_with(**counters):
    base = ConfigStore.fake()
    rows = dict(base._rows)
    for provider, value in counters.items():
        key = f"credits.{provider}"
        rows[key] = ConfigRow(key=key, value=value, updated=TODAY)
    return ConfigStore(rows)


def make(**counters):
    d = fake_deps(settings())
    d.today = lambda: TODAY
    config = config_with(**counters)
    d.config = lambda: config
    return d


def found_line(result):
    return next(line for line in result.message.splitlines() if " found" in line)


USED_UP = {"apollo": "75 / 75 per month", "hunter": "25 / 25 per month",
           "snov": "50 / 50 per month"}


def test_free_plans_run_after_the_others_run_out():
    d = make(**USED_UP, prospeo="0 / 75 per month", tomba="0 / 25 per month")
    result = find_contacts(d, PL)
    assert d.calls == ["prospeo"]  # Apollo, Hunter, Snov have no credits left
    assert ("Ewa Example", "Peer engineer", "Prospeo") in [
        (c.name, c.type, c.source) for c in result.contacts]
    assert ("Piotr Sample", "Hiring", "Prospeo") in [
        (c.name, c.type, c.source) for c in result.contacts]
    assert found_line(result) == (
        "4 of 4 found. Credits: Apollo 75/75, Hunter 25/25, Snov 50/50, Prospeo 3/75, "
        "Tomba 0/25")


def test_tomba_when_prospeo_is_used_up_too():
    d = make(**USED_UP, prospeo="75 / 75 per month", tomba="0 / 25 per month")
    result = find_contacts(d, PL)
    assert d.calls == ["tomba"]
    assert ("Marta Example", "Peer engineer", "Tomba") in [
        (c.name, c.type, c.source) for c in result.contacts]
    assert result.missing == {"Hiring": 1}  # Tomba had no hiring manager
    assert found_line(result).endswith("Prospeo 75/75, Tomba 1/25")


def test_without_a_counter_the_free_plan_is_skipped_and_not_listed():
    d = make(**USED_UP)
    result = find_contacts(d, PL)
    assert d.calls == []
    assert found_line(result) == (
        "2 of 4 found. Missing: 1 peer, 1 hiring. Credits: Apollo 75/75, Hunter 25/25, "
        "Snov 50/50")
    assert "prospeo" not in result.message and "tomba" not in result.message


def test_counters_are_allowlisted_and_simulated_outside_prod():
    check_config_write("credits.prospeo")
    check_config_write("credits.tomba")
    book = CreditBook(config_with(prospeo="1 / 75 per month"), TODAY)
    book.spend("prospeo", 3)
    assert book.available("prospeo") == 71
