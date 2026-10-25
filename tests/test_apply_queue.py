"""Auto-apply step 4 (Autopilot H8, 25 Oct): the queue, 30 a day, 3 forms at a time, the
laptop first ("Is your laptop on?") and Cloud Run after 15 minutes. Still dry runs only."""

import threading
import time
from datetime import datetime, timedelta, timezone

from test_telegram_bot import Clock, settings

from jobengine.apply import queue
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import Reply, fake_desk
from jobengine.sweep.fakes import FAKE_TODAY

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 1, 22, 0, tzinfo=IST)
CONFIG = ConfigStore.from_values({})


def test_thirty_a_day_and_no_job_twice():
    state = FakeBotState()
    assert queue.add(state, CONFIG, [f"j{n}" for n in range(40)], NOW) == (30, 0)
    assert queue.add(state, CONFIG, ["j99"], NOW) == (0, 0)  # today's 30 are used
    assert queue.add(state, CONFIG, ["j0", "new"], NOW + timedelta(days=1)) == (1, 29)
    small = ConfigStore.from_values({"apply.daily_limit": "2"})
    assert queue.add(FakeBotState(), small, ["a", "b", "c"], NOW) == (2, 0)


def test_take_three_and_a_stopped_worker_frees_its_jobs():
    state = FakeBotState()
    queue.add(state, CONFIG, ["a", "b", "c", "d"], NOW)
    assert queue.take(state, "laptop", NOW) == ["a", "b", "c"]
    assert queue.waiting(state, NOW) == ["d"]
    queue.finish(state, "a")
    later = NOW + queue.LEASE + timedelta(minutes=1)
    assert queue.waiting(state, later) == ["b", "c", "d"]  # b and c were never finished


def test_the_laptop_first_then_cloud_run():
    state = FakeBotState()
    assert queue.cloud_decision(state, NOW) == "none"
    queue.add(state, CONFIG, ["a"], NOW)
    queue.laptop_seen(state, NOW)
    assert queue.cloud_decision(state, NOW + timedelta(minutes=5)) == "laptop"
    later = NOW + timedelta(minutes=30)
    assert queue.cloud_decision(state, later) == "ask"
    queue.asked(state, later)
    assert queue.cloud_decision(state, later + timedelta(minutes=5)) == "wait"
    queue.answer(state, True)
    assert queue.cloud_decision(state, later + timedelta(minutes=10)) == "wait"
    assert queue.cloud_decision(state, later + timedelta(minutes=16)) == "cloud"
    queue.answer(state, False)
    assert queue.cloud_decision(state, later + timedelta(minutes=1)) == "cloud"


def test_never_more_than_three_browsers_at_once():
    busy, most, lock = [0], [0], threading.Lock()

    def fill(job):
        with lock:
            busy[0] += 1
            most[0] = max(most[0], busy[0])
        time.sleep(0.05)
        with lock:
            busy[0] -= 1
        if job == "bad":
            raise RuntimeError("form broke")
        return job.upper()

    got = queue.run_parallel(["a", "b", "bad", "c", "d"], fill)
    assert most[0] <= queue.PARALLEL
    assert got[:2] == ["A", "B"] and isinstance(got[2], RuntimeError)


def test_auto_submit_stays_off_until_enough_approved_runs():
    assert queue.submit_status(CONFIG, 3, 0) == ("Auto-submit: off (dry runs only): 3 of 10 dry "
                                                 "runs approved.")
    assert "1 marked wrong" in queue.submit_status(CONFIG, 12, 1)
    assert queue.submit_status(CONFIG, 10, 0).startswith("Auto-submit: ready to discuss")


def make_desk(webhook=False):
    s = settings()
    if webhook:
        s = s.model_copy(update={"bot_mode": "webhook"})
    d = fake_desk(s, FAKE_TODAY, now=Clock())
    d._track_now = lambda: NOW
    for page_id in list(d.repo.rows)[:4]:
        d.repo.rows[page_id]["Status"] = "Resume built"
    d.dry_run_job = lambda ref, form_url=None: [Reply(f"dry run {ref}")]
    return d


def test_the_laptop_bot_fills_three_per_tick():
    d = make_desk()
    [head] = d.autoapply_command("")
    assert head.text.startswith("Queued 4 job(s) for a dry run (filled, never submitted); 26 "
                                "more allowed today.")
    assert "Auto-submit: off (dry runs only): 0 of 10" in head.text
    assert len(d.autoapply_tick()) == 3
    assert len(d.autoapply_tick()) == 1
    assert d.autoapply_tick() == []


def test_cloud_run_asks_about_the_laptop():
    d = make_desk(webhook=True)
    d.autoapply_command("")
    [ask] = d.autoapply_tick()
    assert ask.text.startswith("💻 Is your laptop on? 4 job(s) wait")
    assert [data for _, data in ask.buttons] == ["lp:yes", "lp:no"]
    assert d.autoapply_tick() == []  # waiting for the answer
    assert d.tap("lp:no")[0].text.startswith("OK: Cloud Run fills them now")
    assert len(d.autoapply_tick()) == 3
