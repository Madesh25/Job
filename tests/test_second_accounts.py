"""Second provider accounts (6 Oct): Apollo, Hunter, Snov and Prospeo each may have a second
free account, used only when the first has no credits left or its search fails."""

from datetime import date

from jobengine import http
from jobengine.config_store import ConfigStore
from jobengine.contacts.credits import CreditBook, account_label
from jobengine.contacts.finder import accounts, fake_deps, find_contacts
from jobengine.safety import config_writable_keys
from jobengine.settings import load_settings

TODAY = date(2026, 10, 1)
PL = "fixture-clean-pl"
SECOND = {"APOLLO_API_KEY": "a1", "APOLLO_API_KEY_2": "a2", "HUNTER_API_KEY_2": "h2",
          "SNOV_CLIENT_ID_2": "s2"}


def make(**environ):
    d = fake_deps(load_settings("local", environ))
    d.today = lambda: TODAY
    return d


def used_up(d, key="credits.apollo"):
    config = ConfigStore.fake()
    row = type(config._rows[key])
    config._rows[key] = row(key=key, value="75 / 75 per month", updated=TODAY)
    d.config = lambda: config


def test_accounts_need_every_key_of_the_second_account():
    s = load_settings("local", SECOND)
    assert [name for name, _ in accounts("apollo", s)] == ["apollo", "apollo_2"]
    assert accounts("apollo", s)[1][1].apollo_api_key == "a2"
    assert [name for name, _ in accounts("snov", s)] == ["snov"]  # secret missing
    assert [name for name, _ in accounts("tomba", s)] == ["tomba"]
    assert account_label("hunter_2") == "Hunter-2" and account_label("hunter") == "Hunter"
    assert {"credits.apollo_2", "credits.hunter_2", "credits.snov_2",
            "credits.prospeo_2"} <= set(config_writable_keys)


def test_the_second_account_takes_over_when_the_first_is_used_up():
    d = make(**SECOND)
    used_up(d)
    result = find_contacts(d, PL)
    assert d.calls == ["apollo_2"]
    assert "apollo: no credits left this month, trying the second account" in result.message
    found = [c for c in result.contacts if c.source == "Apollo" and not c.cached]
    assert found and all("found with Apollo-2" in c.notes for c in found)
    assert "Apollo-2 2/75" in result.message  # no Config row: the first account's limit


def test_the_second_account_takes_over_when_the_first_fails():
    d = make(**SECOND)
    real = d.request
    failed = []

    def request(provider):
        inner = real(provider)

        def call(method, url, **kwargs):
            if provider == "apollo" and not failed:
                failed.append(url)
                raise http.HttpError("plan limit", 403)
            return inner(method, url, **kwargs)
        return call

    d.request = request
    result = find_contacts(d, PL)
    assert d.calls == ["apollo", "apollo_2"]
    assert "apollo: not available on this plan (trying the second account)" in result.message
    assert len(result.contacts) == 4


def test_a_first_account_that_answers_is_the_only_one_used():
    d = make(**SECOND)
    find_contacts(d, PL)
    assert d.calls == ["apollo"]


def test_without_a_second_account_nothing_changes():
    d = make()
    used_up(d)
    result = find_contacts(d, PL)
    assert d.calls == ["hunter"]
    assert "apollo: no credits left this month, skipped" in result.message


def test_second_account_counter_from_config_is_written():
    config = ConfigStore.fake()
    row = type(config._rows["credits.apollo"])
    config._rows["credits.hunter_2"] = row(key="credits.hunter_2", value="3 / 25 per month",
                                           updated=TODAY)
    written = []
    book = CreditBook(config, TODAY, lambda key, value, day: written.append((key, value)))
    book.ensure("hunter_2", "hunter")
    book.spend("hunter_2", 1)
    assert written == [("credits.hunter_2", "4 / 25 per month")]
    book.ensure("apollo_2", "apollo")  # no row: counted for this run only, never written
    book.spend("apollo_2", 1)
    assert written == [("credits.hunter_2", "4 / 25 per month")]
    assert "Hunter-2 4/25" in book.line() and "Apollo-2 1/75" in book.line()
