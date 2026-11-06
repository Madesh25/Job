"""/screen asks before it runs when more jobs wait than today's limit leaves (Phase 9 P9-19:
it stopped silently at 30 with 109 jobs waiting)."""

from test_telegram_bot import CHAT_ID, ButtonTelegram, Clock, settings, tap, update

from jobengine import telegram_bot as tb
from jobengine.screen import daily
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY


def desk(limit):
    s = settings()
    s = s.model_copy(update={"screening": {**s.screening, "daily_limit": limit}})
    return s, fake_desk(s, FAKE_TODAY, now=Clock())


def test_more_waiting_than_the_limit_gives_three_choices():
    _s, d = desk(2)
    choice = d.screen_choice()
    # 5 Nov: only the jobs with a description count (one of the 16 waits for /jd)
    assert choice.text.startswith("15 jobs with a description wait to be screened. Today's "
                                  "limit: 2 a day, 2 left")
    assert "1 more have no description yet: they wait for /jd and are not counted." in (
        choice.text)
    # The PC screens on Gemini since 28 Oct: about $0.0006 a job.
    assert "about $0.0006 a job (gemini-3.5-flash-lite), so all 15 cost about $0.01" in (
        choice.text)
    assert choice.buttons == [("Today's 2", "sc:2"), ("4 (over the limit)", "sc:4"),
                              ("All 15", "sc:15")]


def test_no_question_when_the_limit_covers_every_job():
    _s, d = desk(50)
    assert d.screen_choice() is None


def test_limit_used_up_offers_only_over_the_limit():
    _s, d = desk(2)
    daily.record(d.state, FAKE_TODAY, 2)
    assert d.screen_choice().buttons == [("2 (over the limit)", "sc:2"), ("All 15", "sc:15")]


def test_screen_asks_then_the_button_screens_that_many():
    s, d = desk(2)
    fake = ButtonTelegram()
    client = tb.TelegramClient(fake)
    tb.handle_one(client, s, update(1, "/screen all"), desk=d)
    assert fake.buttons[-1] == ["sc:2", "sc:4", "sc:15"]
    assert daily.used_today(d.deps.state, FAKE_TODAY) == 0  # nothing screened before the tap
    tb.handle_one(client, s, tap(2, "sc:4"), desk=d)
    assert daily.used_today(d.deps.state, FAKE_TODAY) == 4  # over today's limit, as asked
    done = next(text for chat, text in fake.sent if "Screening done" in text)
    assert "this run's limit of 4 reached (4 screened today)" in done
    assert all(chat == CHAT_ID for chat, _text in fake.sent)


def test_jobs_without_a_description_never_trigger_the_question():
    """5 Nov: 310 waiting, 270 of them without a description: only 40 could be screened, the
    question offered "All 310"."""
    _s, d = desk(15)
    assert d.screen_choice() is None  # 15 to screen, 15 left today
