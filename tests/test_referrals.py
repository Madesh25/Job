"""Referrals from your LinkedIn connections (flow feature 1, step 2, 23 Oct): who you know at
a job's company, the LinkedIn message, the referral ask by mail, and the free match with the
Contacts table (layer 1). Made-up people at the fixture company Vistula Cloud."""

from datetime import date, timedelta

from test_mail_sender import ready_desk

from jobengine.contacts import connections as conn
from jobengine.contacts.connections import Connection, FakeConnections, StoredRow
from jobengine.contacts.models import Chosen
from jobengine.sweep.fakes import FAKE_TODAY

PIOTR = Connection("Piotr", "Example", "https://www.linkedin.com/in/piotr-ex", "",
                   "Vistula Cloud", "Senior DevOps Engineer")
OLA = Connection("Ola", "Nowak", "https://www.linkedin.com/in/ola-n", "ola.nowak@gmail.com",
                 "Vistula Cloud Sp. z o.o.", "Engineering Manager")
EWA = Connection("Ewa", "Kowalska", "https://www.linkedin.com/in/ewa-k", "",
                 "Vistula Cloud", "Talent Acquisition Partner")
JUNIOR = Connection("Jan", "Young", "https://www.linkedin.com/in/jan-y", "jan@gmail.com",
                    "Vistula Cloud", "Junior DevOps Engineer")
BANK = Connection("Adam", "Bank", "https://www.linkedin.com/in/adam-b", "",
                  "Vistula Bank", "DevOps Engineer")


def rows(*people, info=None):
    return [StoredRow(f"c{i}", p, False, dict((info or {}).get(p.first, {})))
            for i, p in enumerate(people)]


def test_kind_and_company_matching():
    assert [conn.kind(t) for t in ("Senior DevOps Engineer", "Engineering Manager",
                                   "Talent Acquisition Partner", "Sales Director")] == [
        "Engineer", "Manager", "HR", "Other"]
    assert conn.same_company("Capgemini Polska", "Capgemini (IE)")
    assert conn.same_company("Vistula Cloud Sp. z o.o.", "Vistula Cloud")
    assert not conn.same_company("Vistula Bank", "Vistula Cloud")
    assert not conn.same_company("ING", "IN")  # too short to match


def test_the_jobs_country_first_and_no_juniors_or_removed():
    stored = rows(PIOTR, OLA, EWA, JUNIOR, BANK, info={"Ola": {"Country": "Poland"},
                                                        "Piotr": {"Country": "India"}})
    stored.append(StoredRow("gone", Connection("Gone", "X", "", "", "Vistula Cloud",
                                               "SRE"), True))
    found = conn.at_company(stored, "Vistula Cloud", "Poland")
    assert [m.row.person.name for m in found] == ["Ola Nowak", "Ewa Kowalska", "Piotr Example"]
    text = conn.block(found, "Vistula Cloud", "Poland", FAKE_TODAY)
    assert "- Ola Nowak: Engineering Manager [Manager, Poland, has an email]" in text
    assert "- Piotr Example: Senior DevOps Engineer [Engineer, India]" in text


def test_nobody_in_the_country_gets_the_other_office_note():
    found = conn.at_company(rows(PIOTR), "Vistula Cloud", "Poland")
    assert "a referral from another office usually still counts" in conn.block(
        found, "Vistula Cloud", "Poland", FAKE_TODAY)


def test_mailable_needs_an_email_a_known_role_and_no_recent_ask():
    asked = {"Last asked": FAKE_TODAY - timedelta(days=3)}
    found = conn.at_company(rows(PIOTR, OLA, EWA, info={"Ewa": {"Work email": "e@v.example",
                                                                **asked}}),
                            "Vistula Cloud", "Poland")
    assert [m.row.person.name for m in conn.mailable(found, FAKE_TODAY)] == ["Ola Nowak"]
    ewa = next(m for m in found if m.row.person.first == "Ewa")
    assert ewa.email == "e@v.example"  # the work email wins over the export's
    assert ewa.asked_on(FAKE_TODAY) == FAKE_TODAY - timedelta(days=3)


def test_layer_1_fills_only_empty_fields_and_never_a_personal_email():
    store = FakeConnections([PIOTR, OLA])
    store.table["conn-1"]["info"] = {"Country": "Poland"}
    found = conn.at_company(store.rows(), "Vistula Cloud", "Poland")
    named = conn.learn_from_contacts(store, found, [
        Chosen("Piotr Example", "DevOps", "piotr@vistula.example.com", "Peer engineer",
               "Hunter", "Poland"),
        Chosen("Ola Nowak", "EM", "ola@gmail.com", "Hiring", "Hunter", "Ireland")])
    assert named == ["Piotr Example", "Ola Nowak"]
    assert store.table["conn-0"]["info"] == {"Country": "Poland", "Location source": "Contacts",
                                             "Work email": "piotr@vistula.example.com"}
    assert store.table["conn-1"]["info"] == {"Country": "Poland"}  # kept; gmail not a work mail


def test_contacts_answer_lists_your_connections_and_the_referral_flow():
    d = ready_desk(mode="draft")
    d.connections = FakeConnections([PIOTR, OLA, EWA, BANK])
    contacts = d.tap("ct:pl-clean")[-1]
    assert "🤝 Your LinkedIn connections at Vistula Cloud: 3" in contacts.text
    assert ("Referral asks", "rf:pl-clean") in contacts.buttons
    # Piotr Example is also a contact the finder found: his work email and country are kept.
    piotr = next(r for r in d.connections.table.values() if r["person"].first == "Piotr")
    assert piotr["info"].get("Work email", "").endswith("vistula.example.com")

    [asks] = d.tap("rf:pl-clean")
    assert asks.text.startswith("Referral asks for DevOps Engineer at Vistula Cloud: send each "
                                "one yourself as a LinkedIn message")
    assert "https://www.linkedin.com/in/ola-n\nHi Ola, I applied for the DevOps Engineer" in \
        asks.text
    assert asks.buttons and asks.buttons[0][1] == "rm:pl-clean"

    [mailed] = d.tap("rm:pl-clean")
    assert mailed.text.startswith("Added as contacts of this job:")
    assert ("Write Gmail drafts", "dr:pl-clean") in mailed.buttons
    rows_ = {v["Name"]: v for _pid, v in d.contacts.contacts.all_rows()}
    assert rows_["Ola Nowak"]["Type"] == "Peer engineer"
    assert rows_["Ola Nowak"]["Notes"] == "LinkedIn connection (Manager); referral ask"
    ola = next(r for r in d.connections.table.values() if r["person"].first == "Ola")
    assert ola["info"]["Referral"] == "Asked" and ola["info"]["Last asked"] == FAKE_TODAY
    assert d.tap("rm:pl-clean")[0].text.startswith("No connection to mail")
    drafts = d.tap("dr:pl-clean")[0].text
    assert "Ola Nowak (referral ask, peer template)" in drafts


def test_no_connections_table_changes_nothing():
    d = ready_desk(mode="draft")
    d.connections = None
    contacts = d.tap("ct:pl-clean")[-1]
    assert "LinkedIn connections" not in contacts.text
    assert date.today()  # the answer itself is the same as before
