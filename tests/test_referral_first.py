"""Referral first (S7, 23 Nov, your ask): an Apply high job at a company where you have
LinkedIn connections gets the referral suggestion before you apply: on Approve, on the Apply
today card and on the 22:00 nudge card. Made-up people at the fixture company Vistula Cloud."""

from datetime import timedelta

from test_mail_sender import ready_desk
from test_referrals import BANK, EWA, OLA, PIOTR

from jobengine.contacts.connections import FakeConnections
from jobengine.sweep.fakes import FAKE_TODAY


def desk(verdict="Apply high"):
    d = ready_desk(mode="draft", applied=False)
    d.connections = FakeConnections([PIOTR, OLA, EWA, BANK])
    d.repo.rows["pl-clean"]["Screen verdict"] = verdict
    return d


def test_apply_high_with_connections_gets_the_hint_and_button():
    d = desk()
    text, button = d.referral_hint("pl-clean", d.repo.get_values("pl-clean"))
    assert text.startswith("🤝 Referral first: 3 LinkedIn connection(s) at Vistula Cloud (")
    assert "Piotr Example" in text and "Adam Bank" not in text
    assert text.endswith("a referral gets an interview far more often than a cold "
                         "application.")
    assert button == ("🤝 Referral asks: Vistula Cloud", "rf:pl-clean")


def test_no_hint_for_other_verdicts_no_connections_asked_ones_or_switched_off():
    d = desk("Apply normal")
    assert d.referral_hint("pl-clean", d.repo.get_values("pl-clean")) is None
    d = desk()
    d.connections = FakeConnections([BANK])
    assert d.referral_hint("pl-clean", d.repo.get_values("pl-clean")) is None
    d = desk()
    for row in d.connections.table.values():
        row["info"]["Last asked"] = FAKE_TODAY - timedelta(days=3)
    assert d.referral_hint("pl-clean", d.repo.get_values("pl-clean")) is None
    d = desk()
    d.deps.config = lambda: {"apply.referral_first": "false"}
    assert d.referral_hint("pl-clean", d.repo.get_values("pl-clean")) is None


def test_the_nudge_card_puts_the_referral_first():
    d = desk()
    d.repo.rows["pl-clean"]["Status"] = "Resume built"
    d._ready = lambda: ["pl-clean"]
    [card] = d.nudge_card()
    assert "🤝 Referral first: 3 LinkedIn connection(s)" in card.text
    assert card.buttons[0] == ("🤝 Referral asks: Vistula Cloud", "rf:pl-clean")
    assert ("✅ I applied", "ia:pl-clean") in card.buttons


def test_the_apply_today_card_lists_the_referral_hints():
    d = desk()
    d.repo.rows["pl-clean"].update({"Status": "Resume built",
                                    "Posted date": d._nudge_here().date()})
    [card] = d.apply_today()
    assert "1 fresh job(s)" in card.text and "🤝 Referral first: 3" in card.text
    assert ("🤝 Referral asks: Vistula Cloud", "rf:pl-clean") in card.buttons


def test_approve_says_referral_first_before_the_resume():
    d = desk()
    d.repo.rows["pl-clean"]["Status"] = "Screened"
    said = []
    d.notify = said.append
    d.tap("ap:pl-clean")
    texts = [r.text for r in said]
    assert texts[0] == "Approved: Vistula Cloud, DevOps Engineer."
    assert texts[1].startswith("🤝 Referral first: 3 LinkedIn connection(s) at Vistula Cloud")
    assert said[1].buttons == [("🤝 Referral asks: Vistula Cloud", "rf:pl-clean")]
