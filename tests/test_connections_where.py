"""LOG-18 part C (25 Nov, your rules): your LinkedIn connections are ordered by whether they
work in the job's country, not by name; people known to work elsewhere are left out with a
count; where unknown people work comes from one Google search per company (no paid credits),
accepted only when a result names the person with the company."""

from datetime import date

from test_referral_asks import desk as referral_desk
from test_referrals import EWA, OLA, PIOTR

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.contacts import connection_places as places
from jobengine.contacts import connections as conn
from jobengine.contacts.connections import Connection, FakeConnections, StoredRow
from jobengine.llm import LLMError

TODAY = date(2026, 11, 27)


def person(first, position="DevOps Engineer", connected="12 Mar 2024", company="Acme"):
    return Connection(first, "Test", f"https://www.linkedin.com/in/{first.lower()}", "",
                      company, position, connected)


def stored(*people, countries=None):
    countries = countries or {}
    return [StoredRow(f"r-{p.first}", p, info={"Country": countries[p.first]}
                      if p.first in countries else {}) for p in people]


def names(rows, country="Poland"):
    return [m.row.person.first for m in conn.at_company(rows, "Acme", country)]


def test_the_most_likely_helpers_first():
    rows = stored(person("Old"), person("New", connected="02 Feb 2026"),
                  person("Titled", position="SRE, Kraków"), person("Here"),
                  person("India"), countries={"Here": "Poland", "India": "India"})
    assert names(rows) == ["Here", "New", "Titled", "Old"]  # known; sign; unknown; abroad out
    assert conn.abroad(rows, "Acme", "Poland") == {"India": 1}
    assert conn.connected_year(person("X", connected="27 Nov 2026")) == 2026


def test_roles_still_order_inside_a_group():
    rows = stored(person("Hr", position="Talent Acquisition Partner", connected="2026"),
                  person("Eng", position="Platform Engineer", connected="2026"))
    assert names(rows) == ["Eng", "Hr"]


class Llm:
    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.calls = answer, fail, []

    def complete_json_with_search(self, stage, system, user, **kw):
        self.calls.append((stage, user))
        if self.fail:
            raise LLMError("no key")
        return self.answer, []


class Store:
    def __init__(self):
        self.updates = {}

    def update(self, page_id, props):
        self.updates[page_id] = props


def test_one_search_per_company_and_only_answers_that_name_the_company():
    rows = stored(person("Ana"), person("Bo"), person("Cy"), countries={"Cy": "Poland"})
    llm, store, state = Llm({"people": [
        {"name": "Ana Test", "country": "India", "company_seen": True},
        {"name": "Bo Test", "country": "Poland", "company_seen": False},  # maybe someone else
    ]}), Store(), FakeBotState()
    out = places.locate(llm, store, state, rows, "Acme", "Poland", TODAY)
    assert len(llm.calls) == 1 and "Ana Test" in llm.calls[0][1] and "Cy Test" not in \
        llm.calls[0][1]
    assert llm.calls[0][0] == "people"  # Gemini with Google Search (Config model.people)
    assert store.updates == {"r-Ana": {"Country": ("rich_text", "India"),
                                       "Location source": ("select", "Google search")}}
    assert names(out) == ["Cy", "Bo"]  # Ana works in India: out
    assert places.locate(llm, store, state, rows, "Acme", "Poland", TODAY) == rows
    assert len(llm.calls) == 1  # not searched again for 90 days
    later = places.locate(llm, Store(), state, rows, "Acme", "Poland",
                          date(2027, 3, 1))
    assert len(llm.calls) == 2 and later is not None


def test_a_failed_search_changes_nothing_and_is_tried_again():
    rows, state = stored(person("Ana")), FakeBotState()
    assert places.locate(Llm(fail=True), Store(), state, rows, "Acme", "Poland", TODAY) == rows
    assert places.due(state, "Acme", TODAY)


def test_the_referral_message_counts_the_people_abroad():
    d = referral_desk(PIOTR, OLA, EWA)
    base = ConfigStore.fake().all()
    d.deps.config = lambda: ConfigStore.from_values({**base, "connections.locate": "off"})
    for row in d.connections.table.values():
        if row["person"].first == "Piotr":
            row["info"]["Country"] = "India"
    [asks] = d.tap("rf:pl-clean")
    assert "1 more work in India (1): left out, they cannot refer for this job" in asks.text
    assert "Piotr Example" not in asks.text.split("\n\n", 1)[1]
    d.connections = FakeConnections([])
    assert d._abroad_at({"Company": "Vistula Cloud", "Country": "Poland"}) == {}
