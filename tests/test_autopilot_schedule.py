"""PR 12: /autopilot every morning by itself (screen/schedule.py)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from test_telegram_bot import ButtonTelegram, fake_rendering, settings, update
from test_web_app import Recorder, verify
from test_web_app import settings as web_settings

from jobengine import telegram_bot as tb
from jobengine.config_store import ConfigStore
from jobengine.screen import autopilot, schedule
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.web import app as web

WARSAW = ZoneInfo("Europe/Warsaw")


class Clock:
    """An aware clock: 2026-10-01 is a Thursday."""

    def __init__(self, hour=7, minute=5, day=1):
        self.now = datetime(2026, 10, day, hour, minute, tzinfo=WARSAW)

    def __call__(self):
        return self.now


def make_desk(tmp_path, on=True, clock=None, s=None, **config):
    d = fake_desk(s or settings(), FAKE_TODAY, now=clock or Clock())
    fake_rendering(d, tmp_path)
    values = {**ConfigStore.fake().all(), **config}
    if on:
        values[schedule.SWITCH_KEY] = "true"
    store = ConfigStore.from_values(values)
    d.deps.config = lambda: store
    return d


class Fetch:
    def __init__(self):
        self.calls = 0

    def __call__(self, progress):
        self.calls += 1
        return "Job search done: 3 new jobs."


def test_off_by_default(tmp_path):
    d = make_desk(tmp_path, on=False)
    assert not schedule.due(d)
    assert d.autopilot_scheduled(Fetch()) is None
    assert d.autopilot_when()[0].text.startswith(
        "Scheduled autopilot: off. To run /autopilot every morning by itself, set Notion "
        "Config schedule.autopilot to true. It would run Mon to Fri at 07:00 Europe/Warsaw")


def test_runs_once_a_morning(tmp_path):
    clock = Clock(hour=6, minute=59)
    d = make_desk(tmp_path, clock=clock)
    fetch = Fetch()
    assert d.autopilot_scheduled(fetch) is None  # before 07:00
    clock.now = clock.now.replace(hour=7, minute=0)
    replies = d.autopilot_scheduled(fetch)
    assert fetch.calls == 1
    assert replies[0].text.startswith("Scheduled autopilot (07:00 Europe/Warsaw):\n\n"
                                      "Job search done: 3 new jobs.")
    assert "Autopilot done: 6 jobs approved" in replies[0].text
    assert d.state.get(schedule.LAST_KEY) == {"date": "2026-10-01"}
    clock.now = clock.now.replace(hour=8)
    assert d.autopilot_scheduled(fetch) is None  # once a day
    assert fetch.calls == 1
    assert "Today's run has started. Next: Fri 02 Oct 07:00 Europe/Warsaw (10:30 in India)" \
        in d.autopilot_when()[0].text


def test_not_after_the_latest_time_nor_at_the_weekend(tmp_path):
    d = make_desk(tmp_path, clock=Clock(hour=10, minute=1))
    assert not schedule.due(d)
    assert "Next: Fri 02 Oct 07:00" in d.autopilot_when()[0].text
    d = make_desk(tmp_path, clock=Clock(hour=7, minute=30, day=3))  # Saturday
    assert not schedule.due(d)
    assert "Next: Mon 05 Oct 07:00" in d.autopilot_when()[0].text


def test_config_time_and_a_naive_clock(tmp_path):
    # 03:30 UTC is 05:30 in Warsaw (summer time): due with a 05:30 start.
    utc = datetime(2026, 10, 1, 3, 30, tzinfo=ZoneInfo("UTC"))
    d = make_desk(tmp_path, clock=lambda: utc, **{schedule.TIME_KEY: "05:30"})
    assert schedule.due(d)
    d = make_desk(tmp_path, clock=lambda: utc, **{schedule.TIME_KEY: "bad"})
    assert not schedule.due(d)  # falls back to 07:00
    local = datetime(2026, 10, 1, 7, 0)  # a naive clock is the machine's own local time
    d = make_desk(tmp_path, clock=lambda: local)
    assert schedule.local_now(d, schedule.plan(d)).tzinfo is not None


def test_yaml_days_and_times(tmp_path):
    base = settings()
    s = base.model_copy(update={"screening": {**base.screening, "autopilot_schedule": {
        "time": "08:15", "latest": "08:00", "timezone": "Europe/Dublin", "days": ["sat"]}}})
    d = make_desk(tmp_path, s=s, clock=Clock(hour=9, minute=15, day=3))  # 08:15 Dublin
    p = schedule.plan(d)
    assert (p.start.isoformat(), p.latest.isoformat(), p.days_text()) == (
        "08:15:00", "08:15:00", "Sat")
    assert schedule.due(d)


def test_waiting_batch_carries_on_before_a_new_start(tmp_path, monkeypatch):
    d = make_desk(tmp_path)
    d.state.set(autopilot.RUN_KEY, {"since": "x"})
    monkeypatch.setattr(autopilot, "tick", lambda desk: ["carried on"])
    assert d.autopilot_scheduled(Fetch()) == ["carried on"]
    assert d.state.get(schedule.LAST_KEY) is None


def test_long_polling_starts_it(tmp_path):
    d = make_desk(tmp_path)
    fake = ButtonTelegram([[update(1, "/help")]])
    fetch = Fetch()
    tb.run(settings(), tb.TelegramClient(fake), max_polls=1, sleep=lambda s: None,
           fetch=fetch, desk=d)
    assert fetch.calls == 1
    assert any(t.startswith("[LOCAL] Scheduled autopilot (07:00") for _, t in fake.sent)


def test_autopilot_when_command(tmp_path):
    d = make_desk(tmp_path)
    fake = ButtonTelegram([[update(1, "/autopilot when")]])
    tb.poll_once(tb.TelegramClient(fake), settings(), None, desk=d)
    assert fake.sent[-1][1].startswith("[LOCAL] Scheduled autopilot: on, Mon to Fri at 07:00")
    assert d.state.get(schedule.LAST_KEY) is None  # asking never starts a run


def test_webhook_mode_says_the_schedule_carries_on(tmp_path):
    d = make_desk(tmp_path, s=settings().model_copy(update={"bot_mode": "webhook"}))
    assert "The scheduled check (every 30 minutes" in autopilot._wait_text(d)
    d = make_desk(tmp_path, on=False,
                  s=settings().model_copy(update={"bot_mode": "webhook"}))
    assert autopilot._wait_text(d).startswith("Send /autopilot again in about an hour")


def test_cloud_run_task_is_queued_and_reports_in_telegram(tmp_path):
    s = web_settings()
    d = make_desk(tmp_path, s=s)
    telegram = Recorder()
    client = tb.TelegramClient(telegram)
    fetch = Fetch()
    app = web.create_app(s, client, d, web.desk_tasks(s, d, client, fetch), fetch=fetch,
                         verify=verify)
    http = TestClient(app)
    assert http.post("/tasks/autopilot").status_code == 401
    r = http.post("/tasks/autopilot", headers={"Authorization": "Bearer good"})
    assert r.status_code == 202 and r.json() == {"task": "autopilot", "queued": True}
    app.state.worker.join()
    assert fetch.calls == 1
    assert telegram.sent[0].startswith("[LOCAL] Scheduled autopilot (07:00 Europe/Warsaw)")
    sent = len(telegram.sent)
    http.post("/tasks/autopilot", headers={"Authorization": "Bearer good"})
    app.state.worker.join()
    assert fetch.calls == 1 and len(telegram.sent) == sent  # nothing more to do today


def test_cloud_run_task_failure_is_reported(tmp_path):
    s = web_settings()
    d = make_desk(tmp_path, s=s)
    telegram = Recorder()
    client = tb.TelegramClient(telegram)

    def broken(progress):
        raise RuntimeError("Adzuna is down")

    tasks = web.desk_tasks(s, d, client, broken)
    result = tasks.autopilot()
    assert result == {"ok": False, "error": "Adzuna is down"}
    assert telegram.sent == ["[LOCAL] Scheduled autopilot failed: Adzuna is down"]
