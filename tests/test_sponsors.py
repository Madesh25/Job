"""Companies from the visa sponsor lists (28 Oct, your OK): weekly /newcompanies suggestions
for companies on the IND register or the Irish permits list that have jobs for you now.
Made-up companies and boards."""

from datetime import UTC, date, datetime

from test_telegram_bot import Clock, settings

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.ie_permits import PermitList
from jobengine.screen.desk import fake_desk
from jobengine.sweep import discover, sponsors
from jobengine.sweep.fakes import FAKE_TODAY

TODAY = date(2026, 10, 31)  # a Saturday
NONE = ConfigStore.from_values({})


def no_agency(name):
    return "recruit" in name.lower()


def test_saturday_from_11_then_weekly_and_a_switch():
    state = FakeBotState()
    assert not sponsors.due(NONE, state, datetime(2026, 10, 31, 10, 59))
    assert sponsors.due(NONE, state, datetime(2026, 10, 31, 11))
    assert not sponsors.due(NONE, state, datetime(2026, 11, 2, 11))  # the first: a Saturday
    sponsors.mark_done(state, TODAY)
    assert not sponsors.due(NONE, state, datetime(2026, 11, 6, 12))
    assert sponsors.due(NONE, state, datetime(2026, 11, 7, 11))
    off = ConfigStore.from_values({"discover.sponsors": "off"})
    assert not sponsors.due(off, FakeBotState(), datetime(2026, 10, 31, 11))


def test_irish_permits_first_most_permits_first_then_it_like_dutch_names():
    permits = {"Big Bank Ireland DAC": 40, "Cloudy Software Ltd": 120, "Talent Recruit Ltd": 90,
               "Known Corp Limited": 300}
    ind = ["Bakkerij Jansen B.V.", "Dune Data Nederland B.V.", "Polder Cloud B.V.",
           "Old Checked Systems B.V."]
    done = {"old checked systems": "2026-10-01"}
    got = sponsors.candidates(ind, permits, ["Known Corp"], [], done, no_agency)
    assert got == [
        sponsors.Candidate("Cloudy Software Ltd", "Irish permits: 120 last year"),
        sponsors.Candidate("Big Bank Ireland DAC", "Irish permits: 40 last year"),
        sponsors.Candidate("Dune Data Nederland B.V.", sponsors.IND_LABEL),
        sponsors.Candidate("Polder Cloud B.V.", sponsors.IND_LABEL),
    ]  # left out: a Target Company, an agency, checked lately, a bakery
    assert len(sponsors.candidates(ind, permits, [], [], {}, no_agency, limit=2)) == 2


def test_a_check_is_forgotten_after_90_days():
    state = FakeBotState()
    sponsors.remember(state, date(2026, 7, 1), ["Dune Data B.V."])
    sponsors.remember(state, TODAY, ["Polder Cloud B.V."])
    assert sponsors.checked(state, TODAY) == {"polder cloud": "2026-10-31"}


def test_short_names():
    assert sponsors.short_name("Dune Data Nederland B.V.") == "Dune Data Nederland"
    assert sponsors.short_name("Cloudy Software Ltd") == "Cloudy Software"
    assert sponsors.short_name("Big Bank Ireland DAC") == "Big Bank Ireland"


JOBS = {"jobs": [
    {"id": 1, "title": "DevOps Engineer", "location": {"name": "Amsterdam, Netherlands"},
     "absolute_url": "https://boards.greenhouse.io/dunedata/jobs/1"},
    {"id": 2, "title": "Java Developer", "location": {"name": "Amsterdam, Netherlands"},
     "absolute_url": "https://boards.greenhouse.io/dunedata/jobs/2"},
    {"id": 3, "title": "Site Reliability Engineer", "location": {"name": "Austin, TX"},
     "absolute_url": "https://boards.greenhouse.io/dunedata/jobs/3"},
]}


def board_get(boards):
    seen = []

    def get(url, params):
        seen.append(url)
        for token, answer in boards.items():
            if f"/boards/{token}/jobs" in url:
                return answer
        raise http.HttpError("GET failed: HTTP 404", 404)

    get.seen = seen
    return get


def test_the_check_finds_the_board_by_name_and_keeps_your_jobs():
    s = settings()
    fits = sponsors.place_check(s, ConfigStore.from_values(
        {"countries.active": "Poland, Netherlands, Ireland"}))
    get = board_get({"dunedata": JOBS})
    found = sponsors.check(s, sponsors.Candidate("Dune Data Nederland B.V.",
                                                 sponsors.IND_LABEL),
                           get, None, None, TODAY, fits)
    assert found.board_link == "https://boards.greenhouse.io/dunedata"
    assert (found.roles, found.countries) == (["DevOps Engineer"], ["Netherlands"])
    assert not any("linkedin" in url for url in get.seen)
    item = sponsors.item(found, TODAY)
    assert item["name"] == "Dune Data Nederland" and item["sponsor"] == sponsors.IND_LABEL
    assert discover.site_of(item["urls"]) == ("https://boards.greenhouse.io/dunedata",
                                              "Greenhouse")
    none = sponsors.check(s, sponsors.Candidate("Nothing Here B.V.", sponsors.IND_LABEL),
                          board_get({}), None, None, TODAY, fits)
    assert none.roles == [] and none.board_link == ""


def test_sponsor_suggestions_come_first_and_say_why():
    state = FakeBotState()
    found = discover.Found(new={
        "plain co": {"name": "Plain Co", "roles": ["DevOps Engineer"] * 1, "countries": ["Poland"],
                     "urls": ["https://boards.greenhouse.io/plainco/jobs/9"], "boards": ["Adzuna"],
                     "first": "2026-10-31", "last": "2026-10-31"},
        "dune data nederland": {
            "name": "Dune Data Nederland", "roles": ["DevOps Engineer"],
            "countries": ["Netherlands"], "urls": ["https://boards.greenhouse.io/dunedata"],
            "boards": ["visa sponsor list"], "sponsor": sponsors.IND_LABEL,
            "first": "2026-10-30", "last": "2026-10-30"}})
    discover.record(state, found, TODAY)
    order = [v["name"] for _, v in discover.suggestions(state, [])]
    assert order == ["Dune Data Nederland", "Plain Co"]


def saturday():
    return datetime(2026, 10, 31, 6, tzinfo=UTC)  # 11:30 in India


def test_the_bot_suggests_them_on_saturday():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = saturday
    d.permits = lambda: PermitList({"Cloudy Software Ltd": 120})
    d.ind_names = lambda config: ["Dune Data Nederland B.V.", "Bakkerij Jansen B.V."]
    asked = []

    def reader(candidate, today, fits):
        asked.append(candidate.name)
        result = sponsors.Checked(candidate)
        if candidate.name.startswith("Dune"):
            result.board_link = "https://boards.greenhouse.io/dunedata"
            result.roles, result.countries = ["DevOps Engineer"], ["Netherlands"]
        return result

    d.sponsor_reader = reader
    [reply] = d.sponsor_tick()
    assert asked == ["Cloudy Software Ltd", "Dune Data Nederland B.V."]
    assert reply.text.startswith(
        "🛂 Visa sponsor companies: checked 2 from the sponsor lists; 1 have jobs for your "
        "titles in your countries now:\n- Dune Data Nederland (On the Dutch IND sponsor "
        "register): DevOps Engineer")
    card = d.newcompanies_command()[0]
    assert card.text.startswith("🔎 New company 1 of 1: Dune Data Nederland")
    assert "Visa: On the Dutch IND sponsor register\n" in card.text
    assert d.sponsor_tick() == []  # done for the week
    assert set(sponsors.checked(d.state, TODAY)) == {"cloudy software", "dune data"}


def test_nothing_found_sends_nothing():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = saturday
    d.permits = lambda: None
    d.ind_names = lambda config: ["Polder Cloud B.V."]
    d.sponsor_reader = lambda candidate, today, fits: sponsors.Checked(candidate)
    assert d.sponsor_tick() == []
    assert sponsors.checked(d.state, TODAY) == {"polder cloud": "2026-10-31"}
