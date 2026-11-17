"""5 Nov (the autopilot timetable): the mail queue reaches the daily cap and never sends a
mail weeks late: up to 3 per check, follow-ups and the best jobs first, and a mail that
waited 3 working days stays a draft."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.mail import sender, timing

WARSAW = ZoneInfo("Europe/Warsaw")
W = timing.window({}, ConfigStore.from_values({}))


def item(name, template="peer", verdict=None, due=datetime(2026, 10, 5, 8, 0, tzinfo=WARSAW)):
    return timing.QueueItem(
        draft_id=f"d-{name}", job_id="j", page_id=f"c-{name}", name=name, to="me@x.example",
        subject="s", template=template, thread_id="", country="Poland", signature="Thanks,",
        due=due.astimezone(UTC).isoformat(), rank=timing.rank_of(template, verdict))


def test_follow_ups_then_the_best_jobs_then_the_longest_waiting():
    state = FakeBotState()
    early = datetime(2026, 10, 5, 8, 0, tzinfo=WARSAW)
    late = datetime(2026, 10, 6, 8, 0, tzinfo=WARSAW)
    for i in (item("normal-old", verdict="Apply normal", due=early),
              item("low", verdict="Apply low", due=early),
              item("high-new", verdict="Apply high", due=late),
              item("high-old", verdict="Apply high", due=early),
              item("follow", template="followup", due=late)):
        timing.add(state, i)
    now = datetime(2026, 10, 6, 8, 30, tzinfo=WARSAW)  # Tuesday
    assert [i.name for i in timing.due_items(state, now, W)] == [
        "follow", "high-old", "high-new", "normal-old", "low"]


def test_the_window_now_ends_at_11_and_a_mail_stays_a_draft_after_3_working_days():
    assert W.end.isoformat() == "11:00:00"
    assert timing.inside(W, "Poland", datetime(2026, 10, 6, 10, 30, tzinfo=WARSAW))
    state = FakeBotState()
    monday = datetime(2026, 10, 5, 8, 0, tzinfo=WARSAW)
    timing.add(state, item("piotr", verdict="Apply high", due=monday))
    config = ConfigStore.from_values({})
    wednesday = datetime(2026, 10, 7, 9, 0, tzinfo=WARSAW)
    first = timing.load(state)[0]
    assert timing.window_days_missed(W, first, wednesday) == 2  # Monday and Tuesday
    assert sender.drop_stale(state, config, wednesday, W) == []
    thursday = datetime(2026, 10, 8, 9, 0, tzinfo=WARSAW)
    assert sender.drop_stale(state, config, thursday, W) == [
        "Left the mail queue after 3 working days without a free send, kept as drafts in "
        "Gmail (send them yourself or delete them): piotr (referral ask)."]
    assert timing.load(state) == []
    # A weekend does not count, and a held mail never goes stale.
    friday = datetime(2026, 10, 2, 8, 0, tzinfo=WARSAW)
    timing.add(state, item("ola", due=friday))
    assert timing.window_days_missed(W, timing.load(state)[0], monday.replace(hour=9)) == 1
    held = item("ewa")
    held.hold = sender.HOLD
    assert timing.window_days_missed(W, held, thursday) == 0
    # Config mail.stale_days = 5 waits longer.
    five = ConfigStore.from_values({sender.STALE_KEY: "5"})
    assert sender.stale_days(five) == 5 and sender.per_check(five) == 3
