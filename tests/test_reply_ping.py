"""PR 14: a Telegram ping within minutes of a reply (track/ping.py)."""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from test_telegram_bot import ButtonTelegram, Clock, settings
from test_web_app import Recorder, verify
from test_web_app import settings as web_settings

from jobengine import telegram_bot as tb
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import ping
from jobengine.track.fakes import FAKE_NOW
from jobengine.track.models import Message
from jobengine.web import app as web

OCT_1 = datetime(2026, 10, 1, tzinfo=FAKE_NOW.tzinfo)


def make_desk(s=None, since=OCT_1):
    d = fake_desk(s or settings(), FAKE_TODAY, now=Clock())
    d.track.state.set(ping.PINGED_KEY, {"ids": [], "at": since.isoformat()})
    return d


def gmail(d):
    return d.track.gmail()


def texts(d):
    return [r.text for r in d.reply_ping_tick()]


def test_replies_from_contacts_and_employers_are_pinged_once():
    d = make_desk()
    pings = texts(d)
    piotr = next(t for t in pings if "Piotr Example" in t)
    assert piotr.startswith("\U0001F4E9 New reply from Piotr Example (Vistula Cloud)\n"
                            "From: piotr.example@vistula.example.com\n"
                            "Subject: Re: DevOps Engineer on your team at Vistula Cloud\n"
                            "Hi Alex,")
    assert "It may be about an interview or a call: answer soon." in piotr
    assert piotr.endswith("The daily check records it; send /today to record it now.")
    assert "> Hi Piotr" not in piotr  # the quoted mail is cut
    harbor = next(t for t in pings if "talent@harbor.example.com" in t)
    assert "(your application for" in harbor
    # Nothing automatic, no bounce, nothing you sent.
    assert not any("mailer-daemon" in t.lower() or "out of office" in t.lower() for t in pings)
    assert not any("From: madeshwaranm02@gmail.com" in t for t in pings)
    # Not again: the time moved and the IDs are kept.
    assert d.reply_ping_tick() == []  # not due yet
    d.track.state.set(ping.PINGED_KEY, {**d.track.state.get(ping.PINGED_KEY),
                                        "at": "2026-10-01T00:00:00+00:00"})
    assert texts(d) == []


def test_only_every_reply_check_minutes():
    d = make_desk(since=FAKE_NOW - timedelta(minutes=10))
    assert not ping.due(d.track, FAKE_NOW)
    assert d.reply_ping_tick() == []
    assert ping.due(d.track, FAKE_NOW + timedelta(minutes=5))
    d.track.state.delete(ping.PINGED_KEY)
    assert ping.due(d.track, FAKE_NOW)


def test_first_check_looks_back_one_day_and_new_mail_is_pinged():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    assert texts(d) == []  # the fixture replies are older than a day
    assert gmail(d).queries[-1].startswith(
        f"after:{int((FAKE_NOW - timedelta(hours=24)).timestamp())} -in:chats -in:sent")
    gmail(d).threads["t-piotr"].append(Message(
        id="m-new", thread_id="t-piotr", when=FAKE_NOW, sender="piotr.example@vistula.example.com",
        subject="Re: DevOps Engineer", text="Happy to refer you. Send me the job link."))
    d.track.state.set(ping.PINGED_KEY, {**d.track.state.get(ping.PINGED_KEY),
                                        "at": (FAKE_NOW - timedelta(hours=1)).isoformat()})
    (only,) = texts(d)
    assert "Happy to refer you" in only and "interview" not in only


def test_mail_from_strangers_is_not_pinged():
    d = make_desk()
    gmail(d).threads["t-news"] = [Message(
        id="m-news", thread_id="t-news", when=FAKE_NOW, sender="news@shop.example.com",
        subject="Sale", text="Buy now")]
    assert not any("news@shop" in t for t in texts(d))


def test_a_gmail_failure_is_only_logged():
    d = make_desk()

    def broken():
        raise RuntimeError("token expired")

    d.track.gmail = broken
    assert d.reply_ping_tick() == []


def test_polling_loop_and_cloud_run_task():
    d = make_desk()
    fake = ButtonTelegram([])
    tb.autopilot_check(tb.TelegramClient(fake), settings(), d)
    assert any(t.startswith("[LOCAL] \U0001F4E9 New reply from Piotr Example") for _, t in
               fake.sent)
    s = web_settings()
    d = make_desk(s=s)
    telegram = Recorder()
    client = tb.TelegramClient(telegram)
    app = web.create_app(s, client, d, web.desk_tasks(s, d, client, None), verify=verify)
    http = TestClient(app)
    assert http.post("/tasks/replies").status_code == 401
    r = http.post("/tasks/replies", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200 and r.json()["pinged"] >= 2
    assert telegram.sent[0].startswith("[LOCAL] \U0001F4E9 New reply from")
