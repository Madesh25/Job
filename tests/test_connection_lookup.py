"""Location and work email of your LinkedIn connections (feature 1, step 3, 23 Oct): Prospeo
by the LinkedIn profile link and Hunter by name and domain, on demand under a job and with
the credits left on the day before a provider renews (your idea)."""

from dataclasses import dataclass
from datetime import date, timedelta

from test_mail_sender import Clock, fake_rendering, settings

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.contacts import connection_lookup as lookup
from jobengine.contacts.connections import Connection, FakeConnections, StoredRow
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY

ANNA = Connection("Anna", "Test", "https://www.linkedin.com/in/anna-test", "",
                  "Vistula Cloud", "DevOps Engineer")
BOB = Connection("Bob", "Sales", "https://www.linkedin.com/in/bob", "", "Vistula Cloud",
                 "Sales Director")
JUNIOR = Connection("Jan", "Young", "https://www.linkedin.com/in/jan", "", "Vistula Cloud",
                    "Junior SRE")
OTHER = Connection("Ola", "Far", "https://www.linkedin.com/in/ola", "", "Elsewhere Ltd",
                   "Engineering Manager")


@dataclass
class Target:
    name: str
    domain: str | None = None


def row(person, **info):
    return StoredRow("p1", person, False, info)


def test_prospeo_gives_the_country_and_only_a_verified_work_email():
    answer = {"person": {"location": {"country": "Poland", "city": "Kraków"},
                         "email": {"email": "anna@vistula.example.com", "status": "VERIFIED"}}}
    props, found = lookup.prospeo_props(row(ANNA), answer)
    assert found and props == {"Country": ("rich_text", "Poland"),
                               "Location source": ("select", "Prospeo"),
                               "Work email": ("email", "anna@vistula.example.com")}
    gmail = {"person": {"email": {"email": "anna@gmail.com", "status": "VERIFIED"}}}
    assert lookup.prospeo_props(row(ANNA), gmail) == ({}, True)  # a match, nothing to keep
    assert lookup.prospeo_props(row(ANNA), {"error": True, "error_code": "NO_MATCH"}) == (
        {}, False)
    kept = lookup.prospeo_props(row(ANNA, Country="Ireland"), answer)[0]
    assert "Country" not in kept  # only empty fields are filled


def test_prospeo_is_asked_by_the_profile_link():
    calls = []

    def request(method, url, **kw):
        calls.append((method, url, kw["json_body"], kw["headers"]["X-KEY"]))
        return {"error": True, "error_code": "NO_MATCH"}

    lookup.prospeo_lookup(row(ANNA), request, "key-1")
    assert calls == [("POST", lookup.PROSPEO_ENRICH,
                      {"data": {"linkedin_url": "https://www.linkedin.com/in/anna-test"},
                       "only_verified_email": True, "enrich_mobile": False}, "key-1")]


def test_hunter_keeps_a_valid_work_email_only():
    def answer(email, status="valid"):
        return lambda *a, **k: {"data": {"email": email, "verification": {"status": status}}}

    assert lookup.hunter_lookup(row(ANNA), "vistula.example.com",
                                answer("anna@vistula.example.com"), "h") == (
        {"Work email": ("email", "anna@vistula.example.com")}, True)
    assert lookup.hunter_lookup(row(ANNA), "v.example", answer("a@v.example", "invalid"),
                                "h") == ({}, True)
    assert lookup.hunter_lookup(row(ANNA), "v.example", answer(""), "h") == ({}, False)


def test_picks_target_companies_first_and_skip_others():
    rows = [StoredRow("o", OTHER), StoredRow("a", ANNA), StoredRow("b", BOB),
            StoredRow("j", JUNIOR), StoredRow("x", ANNA, True),
            StoredRow("f", ANNA, False, {"Country": "Poland"})]
    targets = [Target("Vistula Cloud", "vistula.example.com")]
    assert [r.page_id for r in lookup.prospeo_picks(rows, targets, 10)] == ["a", "o"]
    assert [(r.page_id, d) for r, d in lookup.hunter_picks(rows, targets, 10)] == [
        ("a", "vistula.example.com"), ("f", "vistula.example.com")]


def test_a_refusal_stops_the_run():
    store = FakeConnections([ANNA, OTHER])
    picks = store.rows()

    def flaky(r):
        if r.page_id == "conn-1":
            raise http.HttpError("prospeo: HTTP 429", 429)
        return {"Country": ("rich_text", "Poland")}, True

    out = lookup.run(store, picks, flaky)
    assert (out.asked, out.filled, out.credits) == (1, 1, 1)
    assert out.notes == ["stopped after 1: prospeo: HTTP 429"]


def test_no_match_for_one_person_skips_only_that_person():
    """Phase 12 P12-03 (28 Oct): one NO_MATCH stopped the whole batch after 0 people."""
    store = FakeConnections([ANNA, OTHER])
    picks = store.rows()

    def lookup_one(r):
        if r.page_id == picks[0].page_id:
            raise http.HttpError("POST https://api.prospeo.io/enrich-person failed: HTTP 400 "
                                 "NO_MATCH", 400)
        return {"Country": ("rich_text", "Poland")}, True

    out = lookup.run(store, picks, lookup_one)
    assert (out.asked, out.filled, out.no_match, out.notes) == (2, 1, 1, [])
    assert lookup.summary("Prospeo", out, "at ING").endswith(
        "1 not known to Prospeo (skipped, no credit).")


def test_the_leftovers_are_used_once_on_the_last_day():
    state = FakeBotState()
    today = date(2026, 11, 22)
    renews = today + timedelta(days=1)
    assert not lookup.leftover_due(state, "prospeo", 40, today + timedelta(days=5), today)
    assert not lookup.leftover_due(state, "prospeo", 0, renews, today)
    assert lookup.leftover_due(state, "prospeo", 40, renews, today)
    lookup.mark_done(state, "prospeo", renews)
    assert not lookup.leftover_due(state, "prospeo", 40, renews, today)
    assert lookup.renewal_from_day(23, date(2026, 10, 23)) == date(2026, 10, 23)
    assert lookup.renewal_from_day(23, date(2026, 12, 24)) == date(2027, 1, 23)
    assert not lookup.checked_today(state, today) and lookup.checked_today(state, today)


def live_desk():
    s = settings(CONTACTS_LIVE="true", PROSPEO_API_KEY="pk", HUNTER_API_KEY="hk")
    d = fake_desk(s, FAKE_TODAY, now=Clock())
    import tempfile
    from pathlib import Path

    fake_rendering(d, Path(tempfile.mkdtemp()))
    d.screen()
    d.connections = FakeConnections([ANNA, OTHER])
    return d


def test_location_and_email_button_asks_prospeo_for_that_company():
    d = live_desk()
    asked = []

    def request(provider):
        def call(method, url, **kw):
            asked.append((provider, kw["json_body"]["data"]["linkedin_url"]))
            return {"person": {"location": {"country": "Poland"}}}
        return call

    d.contacts.request = request
    [reply] = d.connection_lookup_tap("pl-clean")
    assert asked == [("prospeo", "https://www.linkedin.com/in/anna-test")]
    assert reply.text.startswith("Prospeo: 1 of your LinkedIn connections looked up (at "
                                 "Vistula Cloud), 1 filled")
    assert "- Anna Test: DevOps Engineer [Engineer, Poland]" in reply.text


def test_the_leftover_run_spends_prospeo_credits_before_the_renewal():
    d = live_desk()
    calls = []

    def request(provider):
        def call(method, url, **kw):
            calls.append((provider, url))
            if url.endswith("account-information"):
                return {"response": {"remaining_credits": 5, "next_quota_renewal_days": 1}}
            if url.endswith("/account"):
                return {"data": {"requests": {"credits": {"used": 50, "available": 50}},
                                 "reset_date": "2026-11-23"}}
            return {"person": {"location": {"country": "Ireland"}}}
        return call

    d.contacts.request = request
    [reply] = d.leftover_tick()
    assert reply.text.startswith("Prospeo: 2 of your LinkedIn connections looked up (5 credits "
                                 "left, new credits on")
    assert d.leftover_tick() == []  # the balances are asked once a day
    assert [c for c in calls if "enrich" in c[1]] == [("prospeo", lookup.PROSPEO_ENRICH)] * 2
