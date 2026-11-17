"""Safe sending (Phase 12 P12-C1 and C2, 28 Oct): an unverified address stays a draft, a
domain without a mail server too, and sending pauses by itself after bounces."""

from datetime import date, timedelta

from test_mail_sender import ready_desk, statuses

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.mail import send_guard, sender

NONE = ConfigStore.from_values({})
DAY = date(2026, 11, 2)


def test_who_the_bot_may_mail_by_itself():
    rule = send_guard.address_problem
    assert rule({"Status": "Verified", "Source": "Hunter"}, "a@x.example", NONE, None) is None
    assert rule({"Status": "Drafted", "Source": "Careers page",
                 "Notes": "drafted for X (was Unverified)"}, "a@x.example", NONE, None) is None
    assert rule({"Status": "Drafted", "Source": "Hunter",
                 "Notes": "drafted for X (was Unverified)"}, "a@x.example", NONE, None) == (
        "the address is not verified (Hunter): check it and send it from Gmail")
    assert rule({"Status": "Unverified", "Source": "GitHub"}, "a@x.example",
                ConfigStore.from_values({"mail.send_unverified": "true"}), None) is None
    no_mail = {"x.example": False, "y.example": None}.get
    assert rule({"Status": "Verified"}, "a@x.example", NONE, no_mail) == (
        "x.example receives no mail (no mail server in its DNS)")
    assert rule({"Status": "Verified"}, "a@y.example", NONE, no_mail) is None  # unknown


def test_the_mx_look_up_is_cached_and_never_fails_a_mail():
    calls = []

    def get_json(url, params):
        calls.append(params["name"])
        if params["name"] == "broken.example":
            raise OSError("timeout")
        if params["name"] == "gone.example":
            return {"Status": 3}
        if params["name"] == "nomx.example":
            return {"Status": 0, "Answer": [{"type": 1}]}
        return {"Status": 0, "Answer": [{"type": 15, "data": "10 mx.example."}]}

    lookup = send_guard.mx_lookup(get_json)
    assert lookup("acme.example") is True and lookup("acme.example") is True
    assert calls.count("acme.example") == 1
    assert lookup("gone.example") is False and lookup("nomx.example") is False
    assert lookup("broken.example") is None


def test_an_unverified_address_stays_a_draft():
    d = ready_desk()
    d.tap("ct:pl-clean")
    first = d.mail.contacts.all_rows()[-1][0]
    d.mail.contacts.update(first, {"Status": "Unverified", "Source": "Hunter"})
    text = d.tap("dr:pl-clean")[0].text
    assert "Sent 3 of 4" in text
    assert "the address is not verified (Hunter): check it and send it from Gmail" in text
    assert statuses(d)[first][0] == "Drafted"


def test_a_domain_without_mail_servers_stays_a_draft():
    d = ready_desk()
    d.mail.mx = lambda domain: False
    d.tap("ct:pl-clean")
    text = d.tap("dr:pl-clean")[0].text
    assert "Sent 0 of 4" in text and "receives no mail" in text


def test_two_bounces_in_a_day_pause_sending():
    state = FakeBotState()
    for _ in range(10):
        send_guard.record_sent(state, DAY)
    assert send_guard.record_bounces(state, DAY, 1) is None
    alert = send_guard.record_bounces(state, DAY, 1)
    assert alert.startswith("🛑 Sending paused: 2 mails bounced today.")
    assert send_guard.paused(state) == "2 mails bounced today"
    assert send_guard.record_bounces(state, DAY, 1) is None  # told once
    assert send_guard.resume(state, DAY) and send_guard.paused(state) is None


def test_more_than_5_percent_in_a_week_pauses_sending():
    state = FakeBotState()
    for n in range(5):
        for _ in range(6):
            send_guard.record_sent(state, DAY + timedelta(days=n))
    send_guard.record_bounces(state, DAY, 1)
    alert = send_guard.record_bounces(state, DAY + timedelta(days=4), 1)
    assert alert.startswith("🛑 Sending paused: 2 of 30 mails bounced in the last 7 days (7%).")
    old = FakeBotState()
    for _ in range(40):
        send_guard.record_sent(old, DAY)
    send_guard.record_bounces(old, DAY, 1)
    assert send_guard.record_bounces(old, DAY + timedelta(days=3), 1) is None  # 2 of 40


def test_a_pause_keeps_drafts_and_mailmode_send_resumes():
    d = ready_desk()
    d.state.set(send_guard.GUARD_KEY, {"paused": "2 mails bounced today", "days": {}})
    d.tap("ct:pl-clean")
    text = d.tap("dr:pl-clean")[0].text
    assert "Sent 0 of 4" in text and "sending is paused (2 mails bounced today)" in text
    assert d.mail.gmail().sent == []
    again = sender.mode_command(d.state, "send", d.mail)
    assert again.startswith("Sending starts again after the bounce pause.")
    assert send_guard.paused(d.state) is None


def test_each_send_is_counted_for_the_guard():
    d = ready_desk()
    d.tap("ct:pl-clean")
    d.tap("dr:pl-clean")
    days = d.state.get(send_guard.GUARD_KEY)["days"]
    assert sum(v[0] for v in days.values()) == 4
