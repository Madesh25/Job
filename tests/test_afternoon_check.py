"""Flow feature 2 (9 Oct): the afternoon check runs /fetch and /screen by itself."""

from fastapi.testclient import TestClient
from test_autopilot_schedule import Clock, Fetch, make_desk
from test_web_app import Recorder, verify
from test_web_app import settings as web_settings

from jobengine import telegram_bot as tb
from jobengine.jobctl import CONTROL
from jobengine.screen import check
from jobengine.web import app as web


def check_desk(tmp_path, clock, **config):
    return make_desk(tmp_path, on=False, clock=clock,
                     **{check.SWITCH_KEY: "true", **config})


def test_off_by_default(tmp_path):
    d = make_desk(tmp_path, on=False, clock=Clock(hour=15, minute=5))
    assert check.due(d) is None
    assert d.check_scheduled(Fetch()) is None
    assert "Afternoon check: off. To run /fetch and /screen by themselves at 15:00" in \
        d.autopilot_when()[0].text


def test_runs_once_at_each_time_then_screens(tmp_path):
    clock = Clock(hour=14, minute=59)
    d = check_desk(tmp_path, clock)
    fetch = Fetch()
    assert d.check_scheduled(fetch) is None  # not yet
    clock.now = clock.now.replace(hour=15, minute=10)
    replies = d.check_scheduled(fetch)
    assert fetch.calls == 1
    assert replies[0].text.startswith("Afternoon check (15:00 Europe/Warsaw): new jobs since "
                                      "this morning.\n\nJob search done: 3 new jobs.")
    assert replies[1].text.startswith("Screening done:")
    assert d.check_scheduled(fetch) is None and fetch.calls == 1  # once a day


def test_two_times_and_the_window(tmp_path):
    clock = Clock(hour=12, minute=40)
    d = check_desk(tmp_path, clock, **{check.TIMES_KEY: "16:30, 12:30"})
    assert [f"{t:%H:%M}" for t in check.times(d)] == ["12:30", "16:30"]
    fetch = Fetch()
    assert d.check_scheduled(fetch)
    clock.now = clock.now.replace(hour=19, minute=0)  # more than 2 h after 16:30
    assert d.check_scheduled(fetch) is None
    clock.now = clock.now.replace(hour=16, minute=45)
    assert d.check_scheduled(fetch) and fetch.calls == 2


def test_not_at_the_weekend_nor_while_another_run_goes(tmp_path):
    assert check.due(check_desk(tmp_path, Clock(hour=15, minute=5, day=3))) is None  # Sat
    d = check_desk(tmp_path, Clock(hour=15, minute=5))
    with CONTROL.job("/fetch"):
        assert d.check_scheduled(Fetch()) is None
    assert d.check_scheduled(Fetch())  # due again once /fetch is done


def test_autopilot_when_shows_it_on(tmp_path):
    d = check_desk(tmp_path, Clock())
    assert "Afternoon check: on, /fetch and /screen at 15:00 Europe/Warsaw" in \
        d.autopilot_when()[0].text


def test_cloud_run_task(tmp_path):
    s = web_settings()
    d = make_desk(tmp_path, on=False, s=s, clock=Clock(hour=15, minute=5),
                  **{check.SWITCH_KEY: "true"})
    telegram = Recorder()
    client = tb.TelegramClient(telegram)
    fetch = Fetch()
    app = web.create_app(s, client, d, web.desk_tasks(s, d, client, fetch), fetch=fetch,
                         verify=verify)
    http = TestClient(app)
    assert http.post("/tasks/check").status_code == 401
    r = http.post("/tasks/check", headers={"Authorization": "Bearer good"})
    assert r.status_code == 202 and r.json() == {"task": "check", "queued": True}
    app.state.worker.join()
    assert fetch.calls == 1
    assert telegram.sent[0].startswith("[LOCAL] Afternoon check (15:00 Europe/Warsaw)")
