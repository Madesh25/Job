"""PR 17: the daily send cap ramps up week by week (mail/warmup.py)."""

from datetime import date, timedelta

from test_mail_sender import ready_desk

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.mail import sender, warmup
from jobengine.sweep.fakes import FAKE_TODAY

START = date(2026, 10, 1)
YAML = {"warmup": {"steps": [10, 15, 20, 25, 30]}}


def config(**values):
    return ConfigStore.from_values(values)


def state_started(day=START):
    state = FakeBotState()
    state.set(warmup.START_KEY, {"date": day.isoformat()})
    return state


def test_steps_week_by_week():
    state = state_started()
    caps = [warmup.cap(YAML, state, config(), START + timedelta(days=d))
            for d in (0, 6, 7, 14, 21, 28, 90)]
    assert caps == [10, 10, 15, 20, 25, 30, 30]
    assert warmup.cap(YAML, FakeBotState(), config(), START) == 10  # before the first send
    assert warmup.cap(YAML, state, config(**{warmup.SWITCH_KEY: "off"}), START) is None


def test_config_start_wins_and_bad_steps_fall_back():
    state = state_started()
    earlier = config(**{warmup.START_KEY: "2026-09-10 (mailed by hand before)"})
    assert warmup.week(state, earlier, START) == 4
    assert warmup.steps({"warmup": {"steps": ["x"]}}) == warmup.DEFAULT_STEPS
    assert warmup.steps({}) == warmup.DEFAULT_STEPS


def test_first_send_starts_week_one():
    d = ready_desk()
    d.tap("ct:pl-clean")
    assert d.state.get(warmup.START_KEY) is None
    d.tap("dr:pl-clean")
    assert d.state.get(warmup.START_KEY) == {"date": FAKE_TODAY.isoformat()}


def test_cap_is_the_smaller_of_the_step_and_the_ceiling():
    d = ready_desk()
    d.state.set(warmup.START_KEY, {"date": (FAKE_TODAY - timedelta(days=21)).isoformat()})
    base = ConfigStore.fake().all()  # fixture ceiling mail.daily_send_cap 15
    assert sender.daily_cap(d.mail, ConfigStore.from_values(base), d.state) == 15
    raised = ConfigStore.from_values({**base, "mail.daily_send_cap": "30"})
    assert sender.daily_cap(d.mail, raised, d.state) == 25  # week 4
    unset = ConfigStore.from_values({k: v for k, v in base.items()
                                     if k != "mail.daily_send_cap"})
    assert sender.ceiling(d.mail, unset) == 30  # the last step when no ceiling is set
    off = ConfigStore.from_values({**base, warmup.SWITCH_KEY: "off"})
    assert sender.daily_cap(d.mail, off, d.state) == 15
    unset_off = ConfigStore.from_values({k: v for k, v in off.all().items()
                                         if k != "mail.daily_send_cap"})
    assert sender.daily_cap(d.mail, unset_off, d.state) == 10  # the old default


def test_mailmode_shows_the_warmup():
    d = ready_desk()
    text = d.mailmode_command("")[0].text
    assert text.startswith("Mail mode: send.")
    assert ("Warm-up: week 1 (starts with the first mail sent), 10 mails a day today (plan "
            "10, 15, 20, 25, 30 a day, week by week; Config mail.daily_send_cap 15 is the "
            "ceiling).") in text
    d.state.set(warmup.START_KEY, {"date": (FAKE_TODAY - timedelta(days=7)).isoformat()})
    assert "week 2 (since 2026-09-24), 15 mails a day today" in d.mailmode_command("")[0].text
