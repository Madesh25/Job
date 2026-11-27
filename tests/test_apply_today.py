"""Apply today (S4, 23 Nov, your ask): each morning the 3 to 5 best fresh jobs, best first.
Made-up jobs, no Notion."""

from datetime import date, datetime, timedelta, timezone

from test_telegram_bot import CHAT_ID, Clock, settings

from jobengine import telegram_bot as tb
from jobengine.bot_state import FakeBotState
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import apply_today as at

IST = timezone(timedelta(hours=5, minutes=30))
TODAY = date(2026, 11, 24)
MORNING = datetime(2026, 11, 24, 8, 45, tzinfo=IST)


def job(company, verdict="Apply high", days=0, status="Resume built", **more):
    return {"Company": company, "Role": "DevOps Engineer", "Country": "Poland",
            "City": "Warsaw", "Status": status, "Screen verdict": verdict,
            "Posted date": TODAY - timedelta(days=days), **more}


ROWS = [
    ("a", job("Old high", days=3)),  # older than 48 hours
    ("b", job("Normal new", "Apply normal", days=0)),
    ("c", job("High yesterday", days=1)),
    ("d", job("High today", days=0)),
    ("e", job("Low today", "Apply low")),  # not good enough
    ("f", job("Applied", status="Applied")),  # not waiting
    ("g", job("Review", "Needs review")),
    ("h", {**job("No posted date", "Apply normal"), "Posted date": None,
           "First seen": TODAY - timedelta(days=1)}),
    ("i", job("Normal 2 days", "Apply normal", days=2)),
    ("j", job("High 2 days", days=2)),
]


def test_the_best_fresh_ready_jobs_best_first_at_most_five():
    picked = [values["Company"] for _, values in at.pick(ROWS, TODAY)]
    assert picked == ["High today", "High yesterday", "High 2 days", "Normal new",
                      "No posted date"]
    assert [v["Company"] for _, v in at.pick(ROWS, TODAY, n=10)][-1] == "Normal 2 days"
    assert at.pick([], TODAY) == []


def test_the_card_text_and_buttons():
    jobs = [(pid, values, f"r{pid}") for pid, values in at.pick(ROWS, TODAY, n=2)]
    text = at.text(jobs, TODAY, minutes=10)
    assert text.startswith("☀️ Apply today, Tue 24 Nov: 2 fresh job(s), best first.")
    assert "1. 🇵🇱 High today, DevOps Engineer (Warsaw, Poland)\n   Apply high · posted " \
           "today · /applypack rd" in text
    assert "2. 🇵🇱 High yesterday" in text and "posted yesterday" in text
    assert "About 10 minutes each." in text
    assert at.buttons(jobs) == [("✅ I applied: High today", "ia:rd"),
                                ("✅ I applied: High yesterday", "ia:rc")]


def test_once_a_morning_from_08_30_and_the_switch():
    state = FakeBotState()
    assert at.due({}, state, MORNING)
    assert not at.due({}, state, MORNING.replace(hour=8, minute=0))
    assert not at.due({}, state, MORNING.replace(hour=12, minute=0))
    assert at.due({"schedule.apply_today_time": "07:45"}, state, MORNING.replace(minute=0))
    assert not at.due({"schedule.apply_today": "false"}, state, MORNING)
    at.mark_sent(state, MORNING.date())
    assert not at.due({}, state, MORNING + timedelta(minutes=30))
    assert at.due({}, state, MORNING + timedelta(days=1))


def test_applytoday_on_the_desk():
    desk = fake_desk(settings(), FAKE_TODAY, now=Clock())
    update = {"update_id": 1, "message": {"chat": {"id": int(CHAT_ID)}, "text": "/applytoday"}}
    [reply] = tb.desk_replies(update, settings(), desk)
    assert reply.text.startswith("☀️ Apply today")
    today = desk._nudge_here().date()
    pid = next(iter(desk.repo.rows))
    desk.repo.rows[pid].update({"Status": "Resume built", "Screen verdict": "Apply high",
                                "Posted date": today})
    [reply] = tb.desk_replies(update, settings(), desk)
    assert "1 fresh job(s)" in reply.text
    assert reply.buttons[0][1].startswith("ia:")
    assert "/applytoday - the 3 to 5 best fresh jobs" in tb.HELP_TEXT
