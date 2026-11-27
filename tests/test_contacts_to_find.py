"""LOG-18 part B (25 Nov, your rules): screening writes how many contacts a job gets, in the
job's "Contacts to find" column: 3 for Apply high (recruiter, hiring manager, team lead), 2
for Apply low (recruiter, hiring manager), 1 when your connections work there. Your own number
in the column wins, and the contact search uses it."""

from test_referral_asks import desk as referral_desk
from test_referrals import BANK, EWA, JUNIOR, OLA, PIOTR

from jobengine.config_store import ConfigStore
from jobengine.contacts import finder
from jobengine.contacts.connections import FakeConnections
from jobengine.notion_repo import NotionJobsRepo
from jobengine.screen.models import JobRow
from jobengine.screen.runner import contacts_to_find, job_row
from jobengine.settings import load_settings

S = load_settings("local", {})
ROW = JobRow(page_id="p1", company="Vistula Cloud", role="DevOps Engineer", country="Poland")


def on():
    """The fixtures switch it off (their older tests expect contacts.per_job); on here."""
    return ConfigStore.from_values({**ConfigStore.fake().all(), "contacts.find.on": "true"})


def number(verdict, row=ROW, known=None, config=None):
    at = (lambda company, country: known) if known is not None else None
    return contacts_to_find(config or on(), S, verdict, row, at)


def test_the_number_by_verdict():
    assert number("Apply high") == 3
    assert number("Apply low") == 2
    assert number("Skip") is None and number("Unscreened") is None


def test_one_when_your_connections_work_there():
    assert number("Apply high", known=2) == 1
    assert number("Apply low", known=0) == 2


def test_your_own_number_stays_and_config_wins_over_the_default():
    mine = JobRow(page_id="p1", company="Acme", role="SRE", contacts_to_find=4)
    assert number("Apply high", row=mine) is None  # not overwritten
    assert job_row("p1", {"Contacts to find": 4}).contacts_to_find == 4
    config = ConfigStore.from_values({**on().all(), "contacts.find.apply_high": "4"})
    assert number("Apply high", config=config) == 4
    off = ConfigStore.from_values({**on().all(), "contacts.find.on": "false"})
    assert number("Apply high", config=off) is None  # off: contacts.per_job as before


def test_the_mix_for_each_number():
    def parts(n):
        mix = finder.mix_for(n)
        return mix.recruiter, mix.hiring, mix.peer, mix.total

    assert parts(1) == (1, 0, 0, 1)  # the recruiter first
    assert parts(2) == (1, 1, 0, 2)
    assert parts(3) == (1, 2, 0, 3)  # recruiter, hiring manager, team lead
    assert parts(5) == (1, 2, 2, 5)
    assert finder.contacts_wanted({"Contacts to find": 3}) == 3
    assert finder.contacts_wanted({"Contacts to find": None}) is None
    assert finder.contacts_wanted({}) is None


def test_the_desk_counts_helpful_connections_only():
    d = referral_desk(PIOTR, OLA, EWA, JUNIOR, BANK)
    assert d.helpful_connections("Vistula Cloud", "Poland") == 3  # no junior, no other company
    assert d.deps.connections_at == d.helpful_connections
    d.connections = FakeConnections([])
    assert d.helpful_connections("Vistula Cloud", "Poland") == 0


class Client:
    def __init__(self, columns):
        self.columns, self.sent = columns, []

    def request(self, method, path, body=None, params=None):
        if method == "GET":
            return {"properties": {name: {"type": "number"} for name in self.columns}}
        self.sent.append(body)
        return {"id": "p1"}


def test_the_column_is_written_only_where_the_database_has_it():
    old = Client(["Screen verdict"])
    NotionJobsRepo(old, "ds").update("p1", {"Screen verdict": "Apply high",
                                            "Contacts to find": 3})
    assert set(old.sent[0]["properties"]) == {"Screen verdict"}  # prod before go-live
    new = Client(["Screen verdict", "Contacts to find"])
    NotionJobsRepo(new, "ds").update("p1", {"Screen verdict": "Apply high",
                                            "Contacts to find": 3})
    assert new.sent[0]["properties"]["Contacts to find"] == {"number": 3}
