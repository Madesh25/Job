"""The waiting-for-a-description counts (LOG-03, 24 Nov test log: 4, 11, 15, 51 and 266 in
different messages): one rule, only New jobs wait (the jobs set Expired that morning were still
counted), and each message names the jobs it counts. Made-up jobs."""

from test_pending_keywords import make_desk

from jobengine.screen import waiting
from jobengine.screen.models import ScreenSummary
from jobengine.screen.runner import waiting_for_jd
from jobengine.sweep.fakes import FAKE_TODAY


def test_only_new_jobs_wait():
    d = make_desk()
    before = {r.page_id for r in waiting_for_jd(d.repo, d.state)}
    assert before and before == {r.page_id for r in d.jd_rows(recent=False)}
    pid = next(iter(before))
    d.repo.rows[pid]["Status"] = "Expired"  # S5: too old to apply to
    after = {r.page_id for r in waiting_for_jd(d.repo, d.state)}
    assert after == before - {pid}
    waiting.note(d.state, ["pl-clean"], FAKE_TODAY)
    d.repo.rows["pl-clean"].update({"Screen verdict": "Unscreened", "Status": "Declined"})
    assert "pl-clean" not in {r.page_id for r in waiting_for_jd(d.repo, d.state)}


def test_each_message_names_its_scope():
    s = ScreenSummary(waiting_for_jd=15, waiting_scope="found today")
    assert "15 waiting for JD (found today): /jd" in s.short_text()
    assert ", 15 waiting for JD (found today)." in s.text()
    assert "waiting for JD: /jd" in ScreenSummary(waiting_for_jd=2, waiting_scope="").short_text()
    d = make_desk()
    line = d.waiting_line()
    assert line.startswith("2 jobs need the JD, all days (") and line.endswith(
        "; 1 of the last 3 days: /jd")
    report = d.daily_report_text()
    assert "2 need a description, all days (1 of the last 3 days: /jd)" in report
