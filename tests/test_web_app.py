import threading
import time

import pytest
from fastapi.testclient import TestClient

from jobengine import telegram_bot as tb
from jobengine.bot_state import FakeBotState
from jobengine.screen.desk import fake_desk
from jobengine.settings import load_settings
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.web import app as web
from jobengine.web.auth import SECRET_HEADER

CHAT = "111222333"
SECRET = "s3cretS3cretS3cretS3cretS3cret12"
URL = "https://job-engine-dev-abc.a.run.app"
SA = "job-engine-scheduler@job-search-engine-509510.iam.gserviceaccount.com"
PROD_JOBS = "0228ce56-475a-4b60-8d6b-fd2a59297b24"


def settings(env="local"):
    return load_settings(env, {"TELEGRAM_CHAT_ID": CHAT, "TELEGRAM_BOT_TOKEN": "123:fake",
                               "TELEGRAM_WEBHOOK_SECRET": SECRET, "SERVICE_URL": URL,
                               "SCHEDULER_SA_EMAIL": SA})


class Recorder:
    """A fake Bot API: records sendMessage texts."""

    def __init__(self):
        self.sent = []

    def __call__(self, method, payload, timeout):
        if method in ("sendMessage", "sendDocument"):
            self.sent.append(payload.get("text") or payload.get("caption"))
            return {"ok": True, "result": {"message_id": len(self.sent)}}
        return {"ok": True, "result": True}


def verify(token):
    if token != "good":
        raise ValueError("bad token")
    claims = {"good": {"aud": URL, "email": SA, "email_verified": True}}
    return claims[token]


class Harness:
    def __init__(self, tasks=None, desk=None):
        self.s = settings()
        self.telegram = Recorder()
        client = tb.TelegramClient(self.telegram)
        self.desk = desk
        self.calls = []
        tasks = tasks or web.Tasks(
            daily=lambda: self.calls.append("daily") or {"ok": True},
            digest=lambda: self.calls.append("digest") or {"ok": True},
            sweep=lambda: self.calls.append("sweep") or {"ok": True})
        self.app = web.create_app(self.s, client, desk, tasks, state=FakeBotState(),
                                  verify=verify)
        self.http = TestClient(self.app)

    def post_update(self, update, secret=SECRET):
        headers = {SECRET_HEADER: secret} if secret is not None else {}
        return self.http.post("/telegram/webhook", json=update, headers=headers)

    def drain(self):
        self.app.state.worker.join()


def message(update_id, text, chat=CHAT):
    return {"update_id": update_id, "message": {"chat": {"id": int(chat)}, "text": text}}


def test_healthz_says_only_ok():
    r = Harness().http.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


@pytest.mark.parametrize("secret", [None, "wrong"])
def test_webhook_needs_the_secret(secret):
    h = Harness()
    r = h.post_update(message(1, "/help"), secret=secret)
    h.drain()
    assert r.status_code == 401 and h.telegram.sent == []


def test_webhook_answers_through_the_polling_handler():
    h = Harness()
    assert h.post_update(message(1, "/help")).status_code == 200
    h.drain()
    assert len(h.telegram.sent) == 1 and "/fetch" in h.telegram.sent[0]
    assert h.telegram.sent[0].startswith("[LOCAL] Job Engine commands:")


def test_other_chat_is_ignored():
    h = Harness()
    assert h.post_update(message(1, "/help", chat="999")).status_code == 200
    h.drain()
    assert h.telegram.sent == []


def test_same_update_id_is_processed_once():
    h = Harness()
    h.post_update(message(7, "/help"))
    r = h.post_update(message(7, "/help"))
    h.drain()
    assert r.json() == {"ok": True, "duplicate": True}
    assert len(h.telegram.sent) == 1


def test_recent_updates_survive_a_restart():
    state = FakeBotState()
    web.RecentUpdates(state).seen(5)
    assert web.RecentUpdates(state).seen(5) is True
    recent = web.RecentUpdates(state, size=3)
    for i in range(10, 14):
        recent.seen(i)
    assert list(recent.ids) == [11, 12, 13]


def test_webhook_returns_before_a_slow_handler_finishes(monkeypatch):
    release = threading.Event()
    done = []
    monkeypatch.setattr(tb, "handle_one",
                        lambda *a, **k: (release.wait(5), done.append(True)))
    h = Harness()
    started = time.monotonic()
    assert h.post_update(message(3, "/fetch")).status_code == 200
    assert time.monotonic() - started < 1 and done == []
    release.set()
    h.drain()
    assert done == [True]


@pytest.mark.parametrize("path", ["/tasks/daily", "/tasks/digest", "/tasks/sweep"])
def test_tasks_need_a_scheduler_token(path):
    h = Harness()
    assert h.http.post(path).status_code == 401
    assert h.http.post(path, headers={"Authorization": "Bearer forged"}).status_code == 401
    assert h.calls == []
    r = h.http.post(path, headers={"Authorization": "Bearer good"})
    assert r.status_code == 200 and r.json()["task"] == path.rsplit("/", 1)[1]
    assert h.calls == [path.rsplit("/", 1)[1]]


def test_wrong_audience_or_email_is_403(monkeypatch):
    h = Harness()
    for claims in ({"aud": "https://other.run.app", "email": SA, "email_verified": True},
                   {"aud": URL, "email": "x@example.com", "email_verified": True}):
        app = web.create_app(h.s, tb.TelegramClient(h.telegram), None, web.Tasks(
            daily=lambda: {"ok": True}, digest=lambda: {"ok": True}, sweep=lambda: {"ok": True}),
            state=FakeBotState(), verify=lambda token, c=claims: c)
        r = TestClient(app).post("/tasks/daily", headers={"Authorization": "Bearer x"})
        assert r.status_code == 403


def test_failed_task_is_500_and_reported():
    def broken():
        raise RuntimeError("Notion is down")

    h = Harness(tasks=web.Tasks(daily=broken, digest=broken, sweep=broken))
    r = h.http.post("/tasks/daily", headers={"Authorization": "Bearer good"})
    assert r.status_code == 500 and r.json()["error"] == "Notion is down"
    assert h.telegram.sent == ["[LOCAL] Scheduled task daily failed: Notion is down"]


@pytest.fixture
def real_tasks_harness():
    s = settings()
    desk = fake_desk(s, FAKE_TODAY)
    telegram = Recorder()
    client = tb.TelegramClient(telegram)
    fetched = []

    def fetch(progress):
        fetched.append(True)
        return "Sweep done"

    tasks = web.desk_tasks(s, desk, client, fetch)
    app = web.create_app(s, client, desk, tasks, fetch=fetch, verify=verify)
    return TestClient(app), telegram, fetched, desk


def test_sweep_does_nothing_unless_auto_fetch_is_true(real_tasks_harness):
    http, telegram, fetched, desk = real_tasks_harness
    r = http.post("/tasks/sweep", headers={"Authorization": "Bearer good"})
    assert r.json()["skipped"] == "Config schedule.auto_fetch is not true"
    assert fetched == [] and telegram.sent == []
    from jobengine.config_store import ConfigStore

    base = ConfigStore.fake().all()
    desk.deps.config = lambda: ConfigStore.from_values({**base, "schedule.auto_fetch": "true"})
    r = http.post("/tasks/sweep", headers={"Authorization": "Bearer good"})
    assert r.json() == {"task": "sweep", "started": r.json()["started"], "ok": True}
    assert fetched == [True] and telegram.sent == ["[LOCAL] Sweep done"]


def test_daily_task_sends_the_report(real_tasks_harness):
    http, telegram, _, _ = real_tasks_harness
    r = http.post("/tasks/daily", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert telegram.sent[0].startswith("[LOCAL] Daily check 2026-10-15")
    r = http.post("/tasks/digest", headers={"Authorization": "Bearer good"})
    assert telegram.sent[-1].startswith("[LOCAL] Weekly digest")


def test_startup_refuses_a_dev_config_with_a_prod_id():
    s = settings("dev")
    unsafe = s.model_copy(update={"notion_write": {**s.notion_write,
                                                   "job_opportunities": PROD_JOBS}})
    with pytest.raises(SystemExit) as exc:
        web.build_app(unsafe)
    assert exc.value.code == 1
    missing = settings("dev").model_copy(update={"telegram_webhook_secret": None})
    with pytest.raises(SystemExit):
        web.build_app(missing)


def test_polling_bot_refuses_webhook_mode(monkeypatch, capsys):
    s = settings("dev").model_copy(update={"bot_mode": "webhook"})
    monkeypatch.setattr(tb, "get_settings", lambda: s)
    assert tb.main([]) == 1
    assert "BOT_MODE=webhook" in capsys.readouterr().err
