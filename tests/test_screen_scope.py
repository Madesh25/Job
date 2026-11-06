"""6 Nov: /screen takes the jobs today's /fetch found; /screen week the last 7 days;
/screen all (or whole) every job waiting. The autopilot and the batch still take all."""

from datetime import timedelta

from test_telegram_bot import ButtonTelegram, Clock, settings, tap, update

from jobengine import telegram_bot as tb
from jobengine.screen import daily
from jobengine.screen.desk import fake_desk
from jobengine.screen.runner import unscreened_rows
from jobengine.sweep.fakes import FAKE_TODAY


def desk(limit=50):
    s = settings()
    s = s.model_copy(update={"screening": {**s.screening, "daily_limit": limit}})
    return s, fake_desk(s, FAKE_TODAY, now=Clock())


TODAY = ["pl-clean", "pl-b2b"]  # fixture jobs with a description
WEEK = ["nl-ind", "ie-agency", "pl-7-years"]
NO_JD = {"seed-repost", "li-no-jd"}  # these wait for /jd whatever the scope


def unscreened(d):
    return [pid for pid, v in d.repo.rows.items() if v.get("Screen verdict") == "Unscreened"]


def found(d, today=(), week=()):
    """Swept date: today for `today`, 3 days ago for `week`, 20 days ago for the rest."""
    for pid in unscreened(d):
        days = 0 if pid in today else 3 if pid in week else 20
        d.repo.rows[pid]["Swept date"] = FAKE_TODAY - timedelta(days=days)


def test_rows_per_scope():
    _s, d = desk()
    ids = unscreened(d)
    found(d, today=ids[:2], week=ids[2:5])
    assert {r.page_id for r in unscreened_rows(d.deps, FAKE_TODAY, "today")} == set(ids[:2])
    assert {r.page_id for r in unscreened_rows(d.deps, FAKE_TODAY, "week")} == set(ids[:5])
    assert len(unscreened_rows(d.deps, FAKE_TODAY, "all")) == len(ids)
    assert len(unscreened_rows(d.deps, FAKE_TODAY)) == len(ids)  # the default stays all


def test_screen_takes_only_todays_jobs_and_names_the_rest():
    s, d = desk()
    ids = unscreened(d)
    found(d, today=TODAY, week=WEEK)
    fake = ButtonTelegram([[update(1, "/screen")]])
    tb.poll_once(tb.TelegramClient(fake), s, None, desk=d)
    done = next(t for _, t in fake.sent if "Screening done" in t)
    left = set(unscreened(d))
    assert not left & set(TODAY)  # today's jobs were screened
    assert set(ids) - set(TODAY) <= left  # the older ones still wait
    assert f"Only the jobs found today: {len(ids) - 2} older Unscreened jobs not taken " \
           "(/screen week or /screen all for those)." in done


def test_screen_week_and_all():
    s, d = desk()
    ids = unscreened(d)
    found(d, today=TODAY, week=WEEK)
    tb.poll_once(tb.TelegramClient(ButtonTelegram([[update(1, "/screen week")]])), s, None,
                 desk=d)
    assert set(unscreened(d)) == set(ids) - set(TODAY) - set(WEEK)
    tb.poll_once(tb.TelegramClient(ButtonTelegram([[update(2, "/screen whole")]])), s, None,
                 desk=d)
    assert set(unscreened(d)) == NO_JD  # only the jobs waiting for /jd


def test_nothing_found_today_says_how_to_take_the_older_ones():
    s, d = desk()
    found(d)
    fake = ButtonTelegram([[update(1, "/screen")]])
    tb.poll_once(tb.TelegramClient(fake), s, None, desk=d)
    done = next(t for _, t in fake.sent if "Screening done" in t)
    assert "Screening done: 0 screened" in done
    assert "older Unscreened jobs not taken (/screen week or /screen all for those)." in done
    assert daily.used_today(d.deps.state, FAKE_TODAY) == 0


def test_the_limit_question_counts_and_keeps_the_scope():
    s, d = desk(limit=1)
    ids = unscreened(d)
    found(d, today=TODAY + WEEK[:2])
    choice = d.screen_choice("today")
    assert choice.text.startswith("4 jobs with a description wait to be screened (found "
                                  "today). Today's limit: 1 a day, 1 left")
    assert [b for _, b in choice.buttons] == ["sc:1:today", "sc:2:today", "sc:4:today"]
    fake = ButtonTelegram()
    client = tb.TelegramClient(fake)
    tb.handle_one(client, s, update(1, "/screen"), desk=d)
    assert fake.buttons[-1] == ["sc:1:today", "sc:2:today", "sc:4:today"]
    tb.handle_one(client, s, tap(2, "sc:4:today"), desk=d)
    assert not set(unscreened(d)) & set(TODAY + WEEK[:2])
    assert set(ids) - set(TODAY + WEEK[:2]) <= set(unscreened(d))  # older jobs untouched


def test_old_buttons_without_a_scope_still_take_all():
    s, d = desk(limit=1)
    found(d)  # nothing found today
    fake = ButtonTelegram()
    tb.handle_one(tb.TelegramClient(fake), s, tap(1, "sc:2"), desk=d)
    assert daily.used_today(d.deps.state, FAKE_TODAY) == 2


def test_scope_words_and_usage():
    assert tb._screen_scope("/screen") == "today"
    assert tb._screen_scope("/screen Week") == "week"
    assert tb._screen_scope("/screen whole") == "all"
    assert tb._screen_scope("/screen batch") is None
    assert tb._screen_scope("/screen https://example.com/job") is None
    assert tb._screen_scope("/pending") is None
    assert "/screen week" in tb.HELP_TEXT
