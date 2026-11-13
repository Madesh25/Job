"""Fetch idea 3 (13 Nov): which job alert brings mostly old jobs (85 of 125 were too old)."""

from jobengine.sweep.models import SweepSummary


def test_alerts_with_mostly_old_jobs_are_named():
    summary = SweepSummary()
    summary.too_old = 70
    summary.max_age_days = 2
    summary.alert_age = {"LinkedIn": [60, 80], "Glassdoor": [9, 10], "JustJoin IT": [1, 20],
                         "IrishJobs.ie": [3, 4]}
    assert summary.slow_alerts() == [("LinkedIn", 60, 80), ("Glassdoor", 9, 10)]
    text = summary.friendly_text()
    assert ("Mostly old jobs from these alerts: LinkedIn 60 of 80, Glassdoor 9 of 10. Set them "
            "to daily and \"past 24 hours\" on the site.") in text


def test_a_fetch_counts_the_alert_jobs_per_sender():
    from test_telegram_bot import settings

    from jobengine.bot_state import FakeBotState
    from jobengine.sweep.fakes import FAKE_TODAY
    from jobengine.sweep.runner import fake_deps, run_sweep

    s = settings()
    summary = run_sweep(s, fake_deps(s), FAKE_TODAY, state=FakeBotState())
    assert summary.alert_age  # the fake alerts are counted per sender
    assert all(0 <= old <= total for old, total in summary.alert_age.values())
