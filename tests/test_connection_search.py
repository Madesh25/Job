"""Where your LinkedIn connections work, from a Google search (28 Oct): only the person's own
profile link among the search results counts, the country comes from its address or the place
it shows, an email is never looked for or guessed, and LinkedIn is never opened."""

from dataclasses import replace
from datetime import date, datetime

from test_connection_lookup import ANNA, OTHER, live_desk

from jobengine.bot_state import FakeBotState
from jobengine.contacts import connection_search as cs
from jobengine.contacts import connections as conn
from jobengine.contacts.connections import Connection, FakeConnections, StoredRow
from jobengine.llm import LLMError

TODAY = date(2026, 10, 28)
JUNIOR = Connection("Jan", "Young", "https://www.linkedin.com/in/jan", "", "Vistula Cloud",
                    "Junior SRE")
NO_LINK = Connection("Eva", "Plain", "", "", "Vistula Cloud", "SRE")


class Search:
    """Stands in for the AI web search: (answer, result links) per person's name."""

    def __init__(self, answers=None, fail=False):
        self.answers = answers or {}
        self.fail = fail
        self.asked = []

    def complete_json_with_search(self, stage, system, user, **kwargs):
        name = user.splitlines()[0].removeprefix("Person: ")
        self.asked.append((stage, name))
        if self.fail:
            raise LLMError("Gemini call failed")
        return self.answers.get(name, ({"url": ""}, []))


def stored(person, **info):
    return StoredRow("p1", person, False, info)


def test_the_country_comes_from_the_persons_own_result_only():
    own = "https://nl.linkedin.com/in/anna-test"
    llm = Search({"Anna Test": ({"url": own, "title": "Anna Test - SRE - Vistula Cloud | LinkedIn",
                                 "location": "Amsterdam"}, [own])})
    seen = cs.search(llm, stored(ANNA))
    assert (seen.country, seen.other_company) == ("Netherlands", "")
    assert llm.asked == [("people", "Anna Test")]
    # The AI names a profile the search never returned: nothing is trusted.
    unsure = Search({"Anna Test": ({"url": own, "location": "Dublin"},
                                   ["https://www.linkedin.com/in/someone-else"])})
    assert cs.search(unsure, stored(ANNA)) is None


def test_the_place_shown_counts_for_your_three_countries_only():
    own = "https://www.linkedin.com/in/anna-test"
    llm = Search({"Anna Test": ({"url": own, "location": "Kraków, Małopolskie"}, [own])})
    assert cs.search(llm, stored(ANNA)).country == "Poland"
    far = Search({"Anna Test": ({"url": own, "location": "Berlin, Germany"}, [own])})
    assert cs.search(far, stored(ANNA)).country == ""


def test_another_company_in_the_title_means_may_have_left():
    own = "https://pl.linkedin.com/in/anna-test"
    llm = Search({"Anna Test": ({"url": own, "title": "Anna Test - Platform Engineer - Acme"},
                                [own])})
    seen = cs.search(llm, stored(ANNA))
    assert (seen.country, seen.other_company) == ("Poland", "Acme")
    assert cs.title_company("Someone Else - SRE - Acme | LinkedIn", "Anna Test") == ""


def test_no_profile_link_no_search_and_a_failed_search_is_nothing():
    llm = Search()
    assert cs.search(llm, stored(NO_LINK)) is None and llm.asked == []
    assert cs.search(Search(fail=True), stored(ANNA)) is None


def test_a_run_fills_only_an_empty_country_and_waits_30_days():
    own = "https://ie.linkedin.com/in/anna-test"
    store = FakeConnections([ANNA, JUNIOR, NO_LINK, OTHER])
    rows = store.rows()
    llm = Search({"Anna Test": ({"url": own}, [own])})
    state = FakeBotState()
    out = cs.run(store, llm, rows, state, TODAY, limit=10)
    assert out.placed == ["Anna Test (Ireland)"]
    anna = next(r for r in store.rows() if r.person.name == "Anna Test")
    assert anna.info["Country"] == "Ireland" and anna.info["Location source"] == "AI search"
    assert [name for _stage, name in llm.asked] == ["Anna Test", "Ola Far"]  # no junior, no link
    again = cs.run(store, Search(), store.rows(), state, date(2026, 11, 20), limit=10)
    assert again.searched == 0  # Ola was searched 23 days ago, Anna has a country
    later = cs.run(store, Search(), store.rows(), state, date(2026, 11, 28), limit=10)
    assert later.searched == 1


def test_may_have_left_shows_under_the_job():
    state = FakeBotState()
    own = "https://www.linkedin.com/in/anna-test"
    store = FakeConnections([ANNA])
    cs.run(store, Search({"Anna Test": ({"url": own, "title": "Anna Test - SRE - Acme"},
                                        [own])}), store.rows(), state, TODAY, limit=5)
    left = cs.may_have_left(state, TODAY)
    matches = conn.at_company(store.rows(), "Vistula Cloud", "Poland")
    text = conn.block(matches, "Vistula Cloud", "Poland", TODAY, left)
    assert "may have left: the search shows Acme" in text


def test_the_button_searches_first_then_prospeo_for_the_email():
    d = live_desk()
    own = "https://pl.linkedin.com/in/anna-test"
    llm = Search({"Anna Test": ({"url": own}, [own])})
    d.deps = replace(d.deps, llm=lambda config: llm)
    asked = []

    def request(provider):
        def call(method, url, **kw):
            asked.append(provider)
            return {"error": True}  # Prospeo found nobody: no credit
        return call

    d.contacts.request = request
    [reply] = d.connection_lookup_tap("pl-clean")
    assert reply.text.startswith("🔎 Google search: 1 of your connections at Vistula Cloud "
                                 "looked up, 1 now have a country")
    assert "- Anna Test (Poland)" in reply.text and asked == ["prospeo"]
    assert "Anna Test: DevOps Engineer [Engineer, Poland]" in reply.text


def test_the_daily_search_takes_the_companies_of_approved_jobs():
    d = live_desk()
    own = "https://pl.linkedin.com/in/anna-test"
    llm = Search({"Anna Test": ({"url": own}, [own])})
    d.deps = replace(d.deps, llm=lambda config: llm)
    job = next(pid for pid, v in d.repo.rows.items() if v.get("Company") == "Vistula Cloud")
    d.repo.rows[job]["Status"] = "Approved"
    d._track_now = lambda: datetime(2026, 10, 15, 8, 0)  # before 13:00 your time: not yet
    assert d.connection_search_tick() == []
    d._track_now = lambda: datetime(2026, 10, 15, 14, 0)
    [reply] = d.connection_search_tick()
    assert "Anna Test (Poland)" in reply.text
    assert [name for _stage, name in llm.asked] == ["Anna Test"]  # Ola works elsewhere
    assert d.connection_search_tick() == []  # once a day
