"""Referral asks (LOG-02, 24 Nov test log, T23-13): the people shown can be marked as sent, so
the 30-day rule works; the message says how many connections, which company names were
counted, how many juniors were left out and how many are shown. Made-up people at the fixture
company Vistula Cloud."""

from datetime import timedelta

from test_mail_sender import ready_desk
from test_referrals import BANK, EWA, JUNIOR, OLA, PIOTR

from jobengine.contacts import connections as connections_mod
from jobengine.contacts.connections import Connection, FakeConnections
from jobengine.sweep.fakes import FAKE_TODAY


def desk(*people):
    d = ready_desk(mode="draft", applied=False)
    d.connections = FakeConnections(list(people or (PIOTR, OLA, EWA, JUNIOR, BANK)))
    d.repo.rows["pl-clean"]["Screen verdict"] = "Apply high"
    return d


def row(d, first):
    return next(r for r in d.connections.table.values() if r["person"].first == first)


def test_the_header_says_what_was_counted():
    d = desk()
    [asks] = d.tap("rf:pl-clean")
    head = asks.text.split("\n\n")[0].splitlines()
    assert head[1] == ("3 connection(s); counted as Vistula Cloud: Vistula Cloud 2, Vistula "
                       "Cloud Sp. z o.o. 1; 1 junior left out (a junior cannot refer).")
    assert "Adam Bank" not in asks.text  # Vistula Bank is another company
    assert "Showing" not in asks.text  # all 3 shown


def test_showing_n_of_m(monkeypatch):
    monkeypatch.setattr(connections_mod, "MAX_SHOWN", 2)
    d = desk()
    [asks] = d.tap("rf:pl-clean")
    assert ("Showing 2 of 3: the job's country first, then location unknown (connected in "
            "2026 or later, or the place in the title, first); engineers first.") in asks.text
    assert asks.buttons[0] == ("✅ I sent them (2)", "rk:pl-clean")


def test_i_sent_them_marks_the_people_shown_asked():
    d = desk()
    assert d.referral_hint("pl-clean", d.repo.get_values("pl-clean")) is not None
    [asks] = d.tap("rf:pl-clean")
    assert asks.buttons[0] == ("✅ I sent them (3)", "rk:pl-clean")
    assert "tap I sent them: they are marked Asked for 30 days" in asks.text
    [done] = d.tap("rk:pl-clean")
    assert done.text.startswith(f"Marked Asked on {FAKE_TODAY:%d %b} (3 at Vistula Cloud): ")
    for first in ("Piotr", "Ola", "Ewa"):
        assert row(d, first)["info"]["Referral"] == "Asked"
        assert row(d, first)["info"]["Last asked"] == FAKE_TODAY
    assert "Last asked" not in row(d, "Jan")["info"]  # the junior was not shown
    # Asked: Referral first leaves them out, and a second tap marks nobody.
    assert d.referral_hint("pl-clean", d.repo.get_values("pl-clean")) is None
    assert d.tap("rk:pl-clean")[0].text.startswith("Nothing to mark")
    [again] = d.tap("rf:pl-clean")
    assert not any(b[1].startswith("rk:") for b in again.buttons)
    assert "asked on" in again.text


def test_only_the_open_ones_are_marked():
    d = desk()
    row(d, "Ola")["info"]["Last asked"] = FAKE_TODAY - timedelta(days=40)  # over 30 days
    row(d, "Ewa")["info"]["Last asked"] = FAKE_TODAY - timedelta(days=3)
    [asks] = d.tap("rf:pl-clean")
    assert asks.buttons[0] == ("✅ I sent them (2)", "rk:pl-clean")
    d.tap("rk:pl-clean")
    assert row(d, "Ewa")["info"]["Last asked"] == FAKE_TODAY - timedelta(days=3)  # unchanged
    assert row(d, "Ola")["info"]["Last asked"] == FAKE_TODAY


def test_location_and_email_when_some_have_no_country():
    unknown = Connection("Iga", "Example", "https://www.linkedin.com/in/iga-e", "",
                         "Vistula Cloud", "Platform Engineer")
    d = desk(unknown)
    [asks] = d.tap("rf:pl-clean")
    assert ("Location and email", "rl:pl-clean") in asks.buttons
