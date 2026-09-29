"""PR 13: mails go out in the recipient's morning (mail/timing.py and the mail queue)."""

import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from test_mail_sender import ready_desk, statuses
from test_telegram_bot import ButtonTelegram, Clock, fake_rendering, settings, update
from test_web_app import Recorder, verify
from test_web_app import settings as web_settings

from jobengine import telegram_bot as tb
from jobengine.config_store import ConfigStore
from jobengine.gmail_client import GmailError
from jobengine.mail import sender, timing
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.web import app as web

WARSAW = ZoneInfo("Europe/Warsaw")
W = timing.Window(enabled=True, days=(1, 2, 3), start=datetime.min.time().replace(hour=8),
                  end=datetime.min.time().replace(hour=10))


def at(day, hour, minute=0, tz=WARSAW):
    return datetime(2026, 10, day, hour, minute, tzinfo=tz)


def test_window_per_country():
    thursday_830 = at(1, 8, 30)
    assert timing.inside(W, "Poland", thursday_830)
    assert timing.inside(W, "Netherlands", thursday_830)
    assert not timing.inside(W, "Ireland", thursday_830)  # 07:30 in Dublin
    assert timing.next_start(W, "Ireland", thursday_830) == at(1, 8, tz=ZoneInfo("Europe/Dublin"))
    friday = at(2, 9)
    assert timing.next_start(W, "Poland", friday) == at(6, 8)  # next Tuesday
    assert timing.when_text(at(6, 8)) == "Tue 06 Oct 08:00 CEST"
    assert timing.zone("Germany").key == "Europe/Warsaw"  # unknown country


def test_window_from_yaml_and_config():
    config = ConfigStore.from_values({})
    w = timing.window({"send_window": {"days": ["mon", "fri"], "start": "07:30",
                                       "end": "bad"}}, config)
    assert (w.days, w.start.isoformat(), w.end.isoformat()) == ((0, 4), "07:30:00", "10:00:00")
    assert w.text() == "Mon, Fri, 07:30 to 10:00 the recipient's time"
    assert timing.window({}, config).text() == "Tue to Thu, 08:00 to 10:00 the recipient's time"
    assert not timing.window({}, ConfigStore.from_values({timing.WINDOW_KEY: "off"})).enabled


def queued_desk(dry=False):
    d = ready_desk(dry=dry)
    d.mail.now = lambda: at(1, 11)  # Thursday 11:00 in Warsaw: after the window
    d.tap("ct:pl-clean")
    return d


def test_outside_the_window_the_checked_mails_wait():
    d = queued_desk()
    text = d.tap("dr:pl-clean")[0].text
    assert "Sent 0 of 4" in text
    assert ("Waiting for the recipient's morning (/mailqueue shows the queue), then sent by "
            "itself after the same checks:\n- Piotr Example (cold mail): Tue 06 Oct 08:00 CEST"
            ) in text
    assert d.mail.gmail().sent == []
    assert len(timing.load(d.state)) == 4
    assert statuses(d)["fake-contact-9"][0] != "Contacted"
    queue = d.mailqueue_command()[0].text
    assert queue.startswith("Send window: Tue to Thu, 08:00 to 10:00 the recipient's time.\n"
                            "4 mails waiting:\n- Piotr Example (cold mail), Tue 06 Oct 08:00")


def test_the_queue_sends_one_mail_per_check_in_the_morning():
    d = queued_desk()
    d.tap("dr:pl-clean")
    assert d.mail_queue_tick() == []  # still Thursday 11:00
    d.mail.today = lambda: at(6, 8).date()
    d.mail.now = lambda: at(6, 8, 5)
    before = statuses(d)
    first = d.mail_queue_tick()[0].text
    assert first.startswith("Sent at 08:05 CEST (the recipient's morning), after the checks: "
                            "Piotr Example (cold mail) | To madeshwaranm02@gmail.com")
    assert first.endswith("1 of 10 sends used today. 3 mails wait in the queue.")
    assert d.mail.gmail().sent == ["r-fake-draft-1"]
    changed = [v for k, v in statuses(d).items() if v != before[k]]
    assert changed == [("Contacted", "")]
    for _ in range(3):
        d.mail_queue_tick()
    assert len(d.mail.gmail().sent) == 4 and timing.load(d.state) == []
    assert d.mail_queue_tick() == []


def test_a_draft_you_sent_or_changed_leaves_the_queue():
    d = queued_desk()
    d.tap("dr:pl-clean")
    d.mail.now = lambda: at(6, 8, 5)
    gmail = d.mail.gmail()
    real = gmail.draft_raw

    def gone(draft_id):
        if draft_id == "r-fake-draft-1":
            raise GmailError("404")
        return real(draft_id)

    gmail.draft_raw = gone
    assert d.mail_queue_tick()[0].text == (
        "Piotr Example: the draft is no longer in Gmail (sent or deleted), so it left the "
        "mail queue.")
    gmail.changed["r-fake-draft-2"] = b"To: someone@else.example\r\n\r\nhi"
    text = d.mail_queue_tick()[0].text
    assert text.startswith("Not sent, kept as a draft: Rita Example: To is someone@else.example")
    assert gmail.sent == [] and len(timing.load(d.state)) == 2


def test_the_daily_cap_holds_the_queue():
    d = queued_desk()
    d.tap("dr:pl-clean")
    d.mail.now = lambda: at(6, 8, 5)
    d.mail.today = lambda: at(6, 8).date()
    d.state.set(sender.SENT_KEY, {"date": "2026-10-06", "count": 15})
    assert d.mail_queue_tick() == []
    assert len(timing.load(d.state)) == 4


def test_window_off_sends_at_once():
    d = queued_desk()
    base = ConfigStore.fake().all()
    store = ConfigStore.from_values({**base, timing.WINDOW_KEY: "off"})
    d.mail.config = lambda: store
    assert "Sent 4 of 4" in d.tap("dr:pl-clean")[0].text
    assert timing.load(d.state) == []
    assert d.mailqueue_command()[0].text.startswith("Send window: off")


def test_dry_run_only_says_it_would_wait():
    d = queued_desk(dry=True)
    text = d.tap("dr:pl-clean")[0].text
    assert "Would wait for the recipient's morning" in text
    assert timing.load(d.state) == []


def test_mailqueue_command_and_the_polling_loop():
    d = queued_desk()
    d.tap("dr:pl-clean")
    fake = ButtonTelegram([[update(1, "/mailqueue")]])
    tb.poll_once(tb.TelegramClient(fake), settings(), None, desk=d)
    assert fake.sent[-1][1].startswith("[LOCAL] Send window: Tue to Thu")
    assert "mailqueue" in [c["command"] for c in tb.bot_commands()]
    d.mail.now = lambda: at(6, 8, 5)
    tb.autopilot_check(tb.TelegramClient(fake), settings(), d)
    assert fake.sent[-1][1].startswith("[LOCAL] Sent at 08:05 CEST")


def test_cloud_run_mail_task():
    s = web_settings()
    d = queued_desk()
    d.tap("dr:pl-clean")
    d.mail.now = lambda: at(6, 8, 5)
    telegram = Recorder()
    client = tb.TelegramClient(telegram)
    app = web.create_app(s, client, d, web.desk_tasks(s, d, client, None), verify=verify)
    http = TestClient(app)
    assert http.post("/tasks/mail").status_code == 401
    r = http.post("/tasks/mail", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200 and r.json()["sent"] == 1
    assert telegram.sent[0].startswith("[LOCAL] Sent at 08:05 CEST")


def test_autopilot_line_counts_the_waiting_mails():
    d = fake_desk(settings(DRY_RUN="false"), FAKE_TODAY, now=Clock())
    fake_rendering(d, Path(tempfile.mkdtemp()))
    d.mailmode_command("send")
    d.mail.now = lambda: at(1, 11)
    summary = d.autopilot(None)[0].text
    assert ("2. Vistula Cloud, DevOps Engineer (Apply high): resume saved, 4 contacts, "
            "0 of 4 mails sent, 4 wait for the recipient's morning") in summary
