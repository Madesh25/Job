"""Second provider accounts (6 Oct): Apollo, Hunter, Snov and Prospeo (Tomba since 23 Oct)
each may have a second free account, used only when the first has no credits left or its
search fails."""

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
            "credits.prospeo_2", "credits.tomba_2"} <= set(config_writable_keys)


def test_tomba_second_account_needs_key_and_secret():
    # 23 Oct: you made a second Tomba account.
    s = load_settings("local", {"TOMBA_API_KEY": "t1", "TOMBA_API_SECRET": "x1",
                                "TOMBA_API_KEY_2": "t2", "TOMBA_API_SECRET_2": " t2s\r"})
    [_, (name, second)] = accounts("tomba", s)
    assert name == "tomba_2" and (second.tomba_api_key, second.tomba_api_secret) == ("t2", "t2s")
    assert accounts("tomba", load_settings("local", {"TOMBA_API_KEY_2": "t2"}))[1:] == []


def test_real_balances_ask_every_account_with_its_own_key():
    from jobengine.contacts import balance

    s = load_settings("local", {"HUNTER_API_KEY": "h1", "HUNTER_API_KEY_2": "h2",
                                "TOMBA_API_KEY": "t1", "TOMBA_API_SECRET": "x1",
                                "TOMBA_API_KEY_2": "t2", "TOMBA_API_SECRET_2": "x2"})
    asked = []

    def request(method, url, params=None, headers=None, **kwargs):
        if "hunter" in url:
            asked.append(params["api_key"])
            return {"data": {"requests": {"searches": {"used": 5, "available": 25}}}}
        asked.append(headers["X-Tomba-Key"])
        if headers["X-Tomba-Key"] == "t1":
            return {"data": {"requests": {"domains": {"used": 3, "available": 25}}}}
        raise http.HttpError("401 Unauthorized", 401)

    text = balance.lines(s, lambda name: request)
    assert asked == ["h1", "h2", "t1", "t2"]
    assert text == ["Hunter: 20 of 25 searches left this month (from Hunter itself)",
                    "Hunter-2: 20 of 25 searches left this month (from Hunter itself)",
                    "Tomba: 22 of 25 searches left this month (from Tomba itself)",
                    "Tomba-2: could not ask (401 Unauthorized)"]


def test_a_tomba_answer_without_numbers_still_shows_the_key_works():
    from jobengine.contacts import balance

    s = load_settings("local", {"TOMBA_API_KEY": "t1", "TOMBA_API_SECRET": "x1"})
    assert balance.lines(s, lambda name: lambda *a, **k: {"data": {"email": "x"}}) == [
        "Tomba: the key works; Tomba did not say how many searches are left (from Tomba "
        "itself)"]


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
    assert "apollo: not available on this plan or key (" in result.message
    assert "Set as master key\" (trying the second account)" in result.message
    assert len(result.contacts) == 4


def test_a_first_account_that_answers_is_the_only_one_used():
    d = make(**SECOND)
    find_contacts(d, PL)
    assert d.calls == ["apollo"]


def test_without_a_second_account_nothing_changes():
    d = make()
    d.s = d.s.model_copy(update={"contacts": {**d.s.contacts, "location": "country_first"}})
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


def test_a_second_account_is_asked_even_without_the_first_key():
    # 23 Oct /credits: only Tomba-2 had a key, and no Tomba line came.
    from jobengine.contacts import balance

    s = load_settings("local", {"TOMBA_API_KEY_2": "t2", "TOMBA_API_SECRET_2": "x2"})
    answer = {"data": {"requests": {"domains": {"used": 0, "available": 25}}}}
    assert balance.lines(s, lambda name: lambda *a, **k: answer) == [
        "Tomba-2: 25 of 25 searches left this month (from Tomba itself)"]


def test_only_a_second_account_is_used_on_its_own():
    s = load_settings("local", {"TOMBA_API_KEY_2": "t2", "TOMBA_API_SECRET_2": "x2"})
    assert [name for name, _ in accounts("tomba", s)] == ["tomba_2"]
    d = make(TOMBA_API_KEY_2="t2", TOMBA_API_SECRET_2="x2")
    config = ConfigStore.fake()
    row = type(config._rows["credits.apollo"])
    for key in ("credits.apollo", "credits.hunter", "credits.snov", "credits.prospeo"):
        config._rows[key] = row(key=key, value="75 / 75 per month", updated=TODAY)
    d.config = lambda: config
    config._rows["credits.tomba_2"] = row(key="credits.tomba_2", value="0 / 25 per month",
                                          updated=TODAY)
    result = find_contacts(d, PL)
    assert "tomba_2" in d.calls and "tomba" not in d.calls
    assert "Tomba-2 1/25" in result.message
