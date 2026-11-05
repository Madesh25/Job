"""5 Nov (the autopilot timetable): 10 approvals a day, and the 07:00 run leaves a share of
the day's screening and approvals for the 19:30 run, which has the freshest jobs."""

from test_autopilot_schedule import Clock, make_desk

from jobengine.screen import autopilot, daily, schedule


def test_ten_approvals_a_day_and_config_wins(tmp_path):
    d = make_desk(tmp_path, on=False)
    assert autopilot.day_approvals(d) == 10
    assert not autopilot.later_run_today(d)  # schedule off: one run takes the whole day
    assert autopilot.approvals_limit(d) == 10 and autopilot.screening_room(d) is None
    d = make_desk(tmp_path, on=False, **{autopilot.APPROVALS_KEY: "4"})
    assert autopilot.day_approvals(d) == 4


def test_the_morning_run_leaves_the_evening_its_share(tmp_path):
    clock = Clock(hour=7, minute=5)  # Thursday 07:05 India time, 19:30 still ahead
    d = make_desk(tmp_path, clock=clock)
    assert autopilot.later_run_today(d)
    assert autopilot.approvals_limit(d) == 6  # 60% of 10
    assert autopilot.screening_room(d) == 30  # 60% of 50, nothing screened yet
    daily.record(d.state, d.today(), 25)
    assert autopilot.screening_room(d) == 5
    clock.now = clock.now.replace(hour=19, minute=35)  # the evening run: the rest
    assert not autopilot.later_run_today(d)
    assert autopilot.approvals_limit(d) == 10 and autopilot.screening_room(d) is None
    # Config share, and a single start time: no share.
    d = make_desk(tmp_path, clock=Clock(hour=7), **{autopilot.SHARE_KEY: "50"})
    assert autopilot.approvals_limit(d) == 5
    d = make_desk(tmp_path, clock=Clock(hour=7), **{schedule.TIME_KEY: "07:00"})
    assert not autopilot.later_run_today(d)


def test_a_morning_run_that_used_its_share_screens_nothing_more(tmp_path):
    d = make_desk(tmp_path, clock=Clock(hour=7, minute=5))
    daily.record(d.state, d.today(), 30)
    text = autopilot.run(d, None, lambda _text: None)[0].text
    assert "this morning's share (60% of today's limit) is used" in text


def test_the_morning_run_approves_its_share_and_the_rest_waits(tmp_path):
    d = make_desk(tmp_path, clock=Clock(hour=7, minute=5), **{autopilot.APPROVALS_KEY: "2"})
    text = autopilot.run(d, None, lambda _text: None)[0].text
    assert autopilot.approvals_limit(d) == 1
    assert "this morning run approves at most 1 of the day's 2, the evening run the rest" \
        in text
