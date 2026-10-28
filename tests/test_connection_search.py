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
        self.users = []

    def complete_json_with_search(self, stage, system, user, **kwargs):
        name = user.splitlines()[0].removeprefix("Person: ")
        self.asked.append((stage, name))
        self.users.append(user)
        if self.fail:
            raise LLMError("Gemini call failed")
        return self.answers.get(name, ({"url": ""}, []))


def stored(person, **info):
    return StoredRow("p1", person, False, info)


def find(llm, person, domain="", pages=None):
    """The search for one connection, as run() asks it."""
    reader = (lambda url: (url, pages[url])) if pages is not None else None
    return cs.search(llm, person.name, person.company, person.position, person.url, domain,
                     reader)


def test_the_country_comes_from_the_persons_own_result_only():
    own = "https://nl.linkedin.com/in/anna-test"
    llm = Search({"Anna Test": ({"url": own, "title": "Anna Test - SRE - Vistula Cloud | LinkedIn",
                                 "location": "Amsterdam"}, [own])})
    seen = find(llm, ANNA)
    assert (seen.country, seen.other_company) == ("Netherlands", "")
    assert llm.asked == [("people", "Anna Test")]
    # 29 Oct: Google's grounding rarely lists LinkedIn pages. The AI's own answer counts
    # when it is exactly the export's profile link and a search ran; the country then comes
    # from the place shown, never from the address the AI wrote.
    answered = Search({"Anna Test": ({"url": own, "location": "Dublin"},
                                     ["https://example.com/other-page"])})
    seen = find(answered, ANNA)
    assert (seen.profile, seen.country) == (own, "Ireland")
    assert "Their LinkedIn profile: https://www.linkedin.com/in/anna-test" in (
        answered.users[0])
    # Another profile, or no search at all: nothing is trusted.
    other = Search({"Anna Test": ({"url": "https://nl.linkedin.com/in/anna-test-2",
                                   "location": "Dublin"}, ["https://example.com/x"])})
    assert find(other, ANNA) is None
    no_search = Search({"Anna Test": ({"url": own, "location": "Dublin"}, [])})
    assert find(no_search, ANNA) is None


def test_the_place_shown_counts_for_your_three_countries_only():
    own = "https://www.linkedin.com/in/anna-test"
    llm = Search({"Anna Test": ({"url": own, "location": "Kraków, Małopolskie"}, [own])})
    assert find(llm, ANNA).country == "Poland"
    far = Search({"Anna Test": ({"url": own, "location": "Berlin, Germany"}, [own])})
    assert find(far, ANNA).country == ""


def test_another_company_in_the_title_means_may_have_left():
    own = "https://pl.linkedin.com/in/anna-test"
    llm = Search({"Anna Test": ({"url": own, "title": "Anna Test - Platform Engineer - Acme"},
                                [own])})
    seen = find(llm, ANNA)
    assert (seen.country, seen.other_company) == ("Poland", "Acme")
    assert cs.title_company("Someone Else - SRE - Acme | LinkedIn", "Anna Test") == ""


def test_no_profile_link_no_search_and_a_failed_search_is_nothing():
    llm = Search()
    store = FakeConnections([NO_LINK])
    assert cs.run(store, llm, store.rows(), FakeBotState(), TODAY, limit=5).searched == 0
    assert llm.asked == []
    assert cs.search_row(Search(fail=True), stored(ANNA)) is cs.FAILED


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
    assert later.searched == 2  # both again: Anna still has no work email


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
                                 "looked up: 1 now have a country, 0 a work email")
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


TEAM = "https://vistula.example.com/team"
OWN = "https://pl.linkedin.com/in/anna-test"


def with_email(email, page=TEAM, sources=(OWN, TEAM)):
    return Search({"Anna Test": ({"url": OWN, "title": "Anna Test - DevOps - Vistula Cloud",
                                  "email": email, "email_page": page}, list(sources))})


def test_an_email_counts_only_when_the_bot_reads_it_on_that_page():
    pages = {TEAM: "<p>Anna Test, DevOps: anna.test@vistula.example.com</p>"}
    seen = find(with_email("anna.test@vistula.example.com"), ANNA, "vistula.example.com",
                pages)
    assert (seen.email, seen.email_page) == ("anna.test@vistula.example.com", TEAM)
    # Not on the page, a personal address, another domain, a page the search never returned,
    # no page reader: nothing is kept (never guessed).
    for llm, domain in ((with_email("a.test@vistula.example.com"), "vistula.example.com"),
                        (with_email("anna@gmail.com"), "vistula.example.com"),
                        (with_email("anna.test@vistula.example.com"), "other.example.com"),
                        (with_email("anna.test@vistula.example.com", sources=(OWN,)),
                         "vistula.example.com")):
        assert find(llm, ANNA, domain, pages).email == ""
    assert find(with_email("anna.test@vistula.example.com"), ANNA, "vistula.example.com",
                None).email == ""


def test_the_company_site_is_its_email_domain_when_none_is_known():
    assert cs.own_domain("https://careers.vistula.example/team", "Vistula Cloud") == (
        "vistula.example")
    assert cs.own_domain("https://medium.com/@anna", "Vistula Cloud") == ""


def test_a_run_writes_email_and_one_note_but_never_over_yours():
    pages = {TEAM: "<p>anna.test@vistula.example.com</p>"}
    store = FakeConnections([ANNA])
    out = cs.run(store, with_email("anna.test@vistula.example.com"), store.rows(),
                 FakeBotState(), TODAY, limit=5, domain_of=lambda c: "vistula.example.com",
                 read_page=lambda url: (url, pages[url]))
    info = store.rows()[0].info
    assert info["Work email"] == "anna.test@vistula.example.com"
    assert info["Notes"].startswith("Google search 28 Oct: Anna Test - DevOps - Vistula Cloud")
    assert out.emails == ["Anna Test (anna.test@vistula.example.com)"]
    mine = FakeConnections([ANNA])
    first = mine.rows()[0]
    mine.update(first.page_id, {"Notes": ("rich_text", "Met at KubeCon")})
    cs.run(mine, with_email(""), mine.rows(), FakeBotState(), TODAY, limit=5)
    assert mine.rows()[0].info["Notes"] == "Met at KubeCon"


def test_a_contact_without_a_profile_link_needs_name_and_company_in_the_title():
    link = "https://nl.linkedin.com/in/piet-x"
    good = Search({"Piet Smit": ({"url": link, "title": "Piet Smit - SRE - Dune Data"},
                                 [link])})
    assert cs.place(good, "Piet Smit", "Dune Data", "SRE", "Netherlands").profile == link
    assert cs.place(good, "Piet Smit", "Dune Data", "SRE", "Poland") is None
    other = Search({"Piet Smit": ({"url": link, "title": "Piet Smit - SRE - Acme"}, [link])})
    assert cs.place(other, "Piet Smit", "Dune Data", "SRE", "Netherlands") is None
    two = Search({"Piet Smit": ({"url": link, "title": "Piet Smit - SRE - Dune Data"},
                                [link, "https://nl.linkedin.com/in/piet-y"])})
    assert cs.place(two, "Piet Smit", "Dune Data", "SRE", "Netherlands") is None  # which one?


class Anyone:
    """A search that finds every person at the company in Poland."""

    def __init__(self):
        self.asked = []

    def complete_json_with_search(self, stage, system, user, **kwargs):
        lines = dict(line.split(": ", 1) for line in user.splitlines())
        name, company = lines["Person"], lines["Company"]
        self.asked.append(name)
        link = "https://pl.linkedin.com/in/" + name.lower().replace(" ", "-")
        return {"url": link, "title": f"{name} - Engineer - {company}"}, [link]


def test_the_finder_places_hunter_people_by_google_search():
    from test_contacts_free_providers import config_with
    from test_contacts_wider import settings

    from jobengine.contacts import finder
    from jobengine.contacts.providers.base import FixtureHttp

    d = finder.fake_deps(settings(HUNTER_API_KEY="h", CONTACTS_LIVE="true"))
    d.today = lambda: date(2026, 10, 1)
    config = config_with(apollo="75 / 75 per month", snov="50 / 50 per month")
    d.config = lambda: config
    fixture = FixtureHttp(d.s)
    d.request = lambda provider: fixture
    search = Anyone()
    d.llm = lambda config: search
    result = finder.find_contacts(d, "fixture-clean-pl")
    assert search.asked and len(search.asked) <= cs.MAX_CONTACTS
    assert any("location from Google search" in (c.notes or "") for c in result.contacts)
    assert "Google search: looked for where" in result.message


def test_enrich_takes_everyone_but_juniors_target_companies_first():
    bob = Connection("Bob", "Sales", "https://www.linkedin.com/in/bob", "", "Elsewhere Ltd",
                     "Sales Director")
    store = FakeConnections([OTHER, bob, JUNIOR, NO_LINK, ANNA])
    picks = cs.enrich_picks(store.rows(), FakeBotState(), TODAY, ["Vistula Cloud"])
    assert [r.person.name for r in picks] == ["Anna Test", "Ola Far", "Bob Sales"]


def test_enrich_saves_after_each_step_and_stop_keeps_what_is_done():
    from jobengine.jobctl import Stopped

    people = [Connection(f"P{i}", "X", f"https://www.linkedin.com/in/p{i}", "", "Vistula Cloud",
                         "SRE") for i in range(45)]
    store = FakeConnections(people)
    llm = Search({f"P{i} X": ({"url": f"https://pl.linkedin.com/in/p{i}"},
                              [f"https://pl.linkedin.com/in/p{i}"]) for i in range(45)})
    state = FakeBotState()
    steps = []

    def step(out, total):
        steps.append((out.searched, total))
        if len(steps) == 2:
            raise Stopped("/enrich")  # /end after the second step

    picks = cs.enrich_picks(store.rows(), state, TODAY)
    try:
        cs.enrich(store, llm, picks, state, TODAY, step=step)
    except Stopped:
        pass
    assert steps == [(20, 45), (40, 45)]
    placed = [r for r in store.rows() if r.info.get("Country") == "Poland"]
    assert len(placed) == 40
    # The next /enrich goes on with the 5 left.
    assert len(cs.enrich_picks(store.rows(), state, TODAY)) == 5


def test_the_enrich_command_uses_only_the_google_search():
    d = live_desk()
    own = "https://pl.linkedin.com/in/anna-test"
    llm = Search({"Anna Test": ({"url": own}, [own])})
    d.deps = replace(d.deps, llm=lambda config: llm)
    paid = []
    d.contacts.request = lambda provider: paid.append(provider)
    lines = []
    [reply] = d.enrich_command("", lines.append)
    assert reply.text.startswith("🔎 Google search: 2 of your connections in the 🤝 LinkedIn "
                                 "Connections table looked up: 1 now have a country")
    assert "Still without a country: 1; without a work email: 2" in reply.text
    assert reply.buttons == [] and paid == []  # never Hunter or Prospeo
    assert lines[-1] == "2 of 2 connections looked up: 1 countries, 0 work emails found"
    [again] = d.enrich_command("")
    assert again.text.startswith("Every connection worth a search")


def test_a_paused_ai_stops_the_run_and_marks_nobody_searched():
    from jobengine.llm import LLMPausedError

    class Paused:
        def complete_json_with_search(self, *args, **kwargs):
            raise LLMPausedError("AI paused: the month's cap is reached")

    store = FakeConnections([ANNA, OTHER])
    state = FakeBotState()
    out = cs.enrich(store, Paused(), cs.enrich_picks(store.rows(), state, TODAY), state, TODAY)
    assert out.stopped.startswith("AI paused") and out.searched == 0
    assert "Stopped early: AI paused" in cs.summary(out, "here")
    assert len(cs.enrich_picks(store.rows(), state, TODAY)) == 2  # nobody marked searched
    # One failed call is tried again next time, the others go on.
    flaky = Search(fail=True)
    out = cs.enrich(store, flaky, cs.enrich_picks(store.rows(), state, TODAY), state, TODAY)
    assert (out.searched, out.failed) == (2, 2)
    assert len(cs.enrich_picks(store.rows(), state, TODAY)) == 2
    assert cs.place(flaky, "Piet Smit", "Dune Data", "SRE", "Netherlands") is None


def test_searches_before_the_matching_fix_are_tried_again():
    state = FakeBotState()
    state.set(cs.KEY, {"people": {"in/anna-test": {"date": "2026-10-28"}}})
    store = FakeConnections([ANNA])
    assert len(cs.enrich_picks(store.rows(), state, TODAY)) == 1
