from datetime import date

import httpx
import pytest

from jobengine import http
from jobengine.config_store import ConfigStore
from jobengine.contacts.finder import (
    NONE_FOUND,
    answer_domain,
    domain_question,
    fake_deps,
    find_contacts,
    find_domain,
    valid_domain,
)
from jobengine.contacts.providers.base import FixtureHttp
from jobengine.reference import Reference
from jobengine.settings import load_settings

TODAY = date(2026, 10, 1)
PL, IE, NO_DOMAIN = "fixture-clean-pl", "fixture-clean-ie", "fixture-no-domain"


def make(env="local", **environ):
    d = fake_deps(load_settings(env, environ))
    d.today = lambda: TODAY
    return d


def emails(d):
    return [row.get("Email", "").lower() for _, row in d.contacts.all_rows()]


def test_cache_that_fills_the_mix_costs_nothing():
    d = make()
    result = find_contacts(d, IE)
    assert d.calls == []
    assert len(result.contacts) == 4 and all(c.cached for c in result.contacts)
    assert result.message.endswith("4 of 4 found. Credits: Apollo 0/75, Hunter 0/25, Snov 0/50")


def test_cache_jd_then_apollo_fills_the_mix_and_stops():
    d = make()
    result = find_contacts(d, PL)
    assert d.calls == ["apollo"]
    assert [(c.name, c.type, c.source) for c in result.contacts] == [
        ("Kasia Example", "Peer engineer", "Apollo"),
        ("Ola Example", "Recruiter/TA", "Job posting"),
        ("Anna Example", "Peer engineer", "Apollo"),
        ("Piotr Example", "Hiring", "Apollo"),
    ]
    assert result.contacts[0].cached
    lines = result.message.splitlines()
    assert lines[0] == "Contacts for Vistula Cloud, DevOps Engineer (Poland)"
    assert "Peer engineer: Kasia Example (Platform Engineer) [cache]" in lines
    assert "Peer engineer: Anna Example (Senior DevOps Engineer) [Apollo, verified]" in lines
    assert "Recruiter/TA: Ola Example [Job posting]" in lines
    assert "Mailbox: careers@vistula.example.com [Job posting]" in lines
    assert lines[-1] == "4 of 4 found. Credits: Apollo 2/75, Hunter 0/25, Snov 0/50"


def test_writes_new_contacts_links_cached_ones_and_updates_the_job():
    d = make()
    before = dict(d.contacts.rows["ct-fresh-peer"])
    find_contacts(d, PL)
    kasia = d.contacts.rows["ct-fresh-peer"]
    assert kasia["Related jobs"] == [PL]
    assert {k: v for k, v in kasia.items() if k != "Related jobs"} == {
        k: v for k, v in before.items() if k != "Related jobs"}  # never overwritten
    created = [w[2] for w in d.contacts.writes if w[0] == "create"]
    names = {c["Name"]: c for c in created}
    assert set(names) == {"Ola Example", "careers@vistula.example.com", "Anna Example",
                          "Piotr Example"}
    anna = names["Anna Example"]
    assert (anna["Status"], anna["Source"], anna["Type"], anna["Country"]) == (
        "Verified", "Apollo", "Peer engineer", "Poland")
    assert anna["Date found"] == TODAY and anna["Related jobs"] == [PL]
    assert "phone" not in str(anna).lower() and "linkedin" not in str(anna).lower()
    assert names["careers@vistula.example.com"]["Type"] == "Other"
    job = d.jobs.rows[PL]
    assert len(job["Contacts"]) == 5
    assert job["Contact source"] == "Job posting"
    assert job["Contact person"] == "Kasia Example, Ola Example, Anna Example, Piotr Example"


def test_rerun_only_fills_missing_slots():
    d = make()
    find_contacts(d, PL)
    count = len(d.contacts.rows)
    again = find_contacts(d, PL)
    assert len(d.contacts.rows) == count
    assert d.calls == ["apollo"]  # second run: everything from the cache
    assert len(again.contacts) == 4


def blocked_apollo(d):
    fixture = FixtureHttp(d.s)

    def request(provider):
        if provider != "apollo":
            return fixture

        def refuse(method, url, **kw):
            raise http.HttpError(f"{method} {http.safe_url(url)} failed: HTTP 403", 403)
        return refuse

    d.request = request


def test_waterfall_goes_on_to_hunter_and_stops_when_full():
    d = make()
    blocked_apollo(d)
    result = find_contacts(d, PL)
    assert d.calls == ["apollo", "hunter"]
    assert "apollo: not available on this plan" in result.message
    new = [(c.name, c.source) for c in result.contacts if not c.cached]
    assert ("Anna Example", "Hunter") in new and ("Hanna Example", "Hunter") in new
    assert "private.person@gmail.com" not in emails(d)  # personal address dropped
    assert "far.away@other.example.org" not in emails(d)  # off-domain dropped


def test_waterfall_reaches_snov_for_the_recruiter_and_dedupes_across_providers():
    d = make()
    d.jobs.rows[PL]["body"] = ["Description source: ats", "We run EKS. No contact here."]
    result = find_contacts(d, PL)
    assert d.calls == ["apollo", "hunter", "snov"]
    assert [c.name for c in result.contacts if c.type == "Recruiter/TA"] == ["Rita Example"]
    assert emails(d).count("anna.example@vistula.example.com") == 1  # Apollo and Hunter
    assert result.missing == {}


def test_used_up_counter_skips_the_provider():
    d = make()
    config = ConfigStore.fake()
    config._rows["credits.apollo"] = type(config._rows["credits.apollo"])(
        key="credits.apollo", value="75 / 75 per month", updated=TODAY)
    d.config = lambda: config
    result = find_contacts(d, PL)
    assert d.calls == ["hunter"]
    assert "apollo: no credits left this month, skipped" in result.message


def test_no_provider_http_in_dev_and_nothing_in_prod_dry_run():
    calls = []
    http.set_transport(httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)))
    try:
        d = make("dev")
        find_contacts(d, PL)  # dev: fixtures only
        assert calls == [] and d.calls == ["apollo"]
        d = make("prod", DRY_RUN="true")
        d.jobs.rows[PL]["body"] = ["Description source: ats", "No contact here."]
        result = find_contacts(d, PL)
        assert calls == [] and d.calls == []
        assert "apollo: paid calls are off (DRY_RUN), skipped" in result.message
    finally:
        http.set_transport(None)


def test_zero_found():
    d = make("prod", DRY_RUN="true")
    d.contacts.rows.clear()
    d.jobs.rows[PL]["body"] = ["Description source: ats", "No contact here."]
    result = find_contacts(d, PL)
    assert NONE_FOUND in result.message
    assert d.jobs.rows[PL]["Contact source"] == "Not found"


def test_unknown_domain_asks_and_resumes_on_reply():
    d = make()
    result = find_contacts(d, NO_DOMAIN)
    assert result.status == "waiting_domain" and d.calls == []
    assert result.message == domain_question("Mystery Example Labs", NO_DOMAIN)
    assert "Ref JOB-fixturen" in result.message
    assert "Contacts" not in d.jobs.rows[NO_DOMAIN]  # the lookup waits
    bad = answer_domain(d, result.message, "mysteryexamplelabs.teamtailor.com")
    assert isinstance(bad, str) and "not look like" in bad
    resumed = answer_domain(d, result.message, "https://www.mystery.example.com/")
    # The fixtures only hold Vistula people, so every provider runs and finds nobody on the
    # new domain: their emails are off-domain and dropped.
    assert resumed.status == "done" and d.calls == ["apollo", "hunter", "snov"]
    assert "0 of 4 found" in resumed.message or NONE_FOUND in resumed.message
    assert d.state.get("domain:mystery example labs") == {"domain": "mystery.example.com"}
    assert answer_domain(d, "an unrelated message", "x.example.com") is None


def test_domain_rules():
    assert valid_domain("Example.COM") == "example.com"
    assert valid_domain("https://www.example.org/jobs") == "example.org"
    for bad in ("acme.greenhouse.io", "jobs.lever.co", "gmail.com", "not a domain", "x"):
        assert valid_domain(bad) is None
    domains = "Mystery Example Labs = labs.example.com"
    config = ConfigStore.from_values({"contacts.domains": domains})
    assert find_domain("Mystery Example Labs", Reference.fake(), config,
                       make().state) == "labs.example.com"
    assert find_domain("Vistula Cloud", Reference.fake(), config,
                       make().state) == "vistula.example.com"


def test_credit_counters_written_in_prod_only():
    from jobengine.notion_repo import config_writer_for

    assert config_writer_for(load_settings("dev", {}), None, ConfigStore.fake()) is None
    writes = []
    d = make()
    d.config_writer = lambda config: lambda key, value, day: writes.append((key, value))
    find_contacts(d, PL)
    assert writes == [("credits.apollo", "2 / 75 per month")]


def test_no_write_mode():
    d = make()
    d.write = False
    find_contacts(d, PL)
    assert d.contacts.writes == [] and "Contacts" not in d.jobs.rows[PL]


@pytest.mark.parametrize("env", ["local", "dev"])
def test_config_writer_refuses_other_keys(env):
    from jobengine.safety import SafetyError, check_config_write

    with pytest.raises(SafetyError):
        check_config_write("model.tailor")
