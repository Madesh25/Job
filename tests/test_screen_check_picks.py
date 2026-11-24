"""Screening check picks and Why (LOG-04, 24 Nov test log, T23-09): Qualtrics (Expired since
10 Nov) and EY (closed that morning) were picked, and the Why of Skip rows showed only the
ghost risk. Made-up jobs."""

from datetime import timedelta

from test_screen_check import SUNDAY, jobs
from test_telegram_bot import Clock, settings

from jobengine.notion_repo import FakeTable
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import screen_check as sc

BODY = ["Description source: company site", "We need 6 years of Kubernetes.",
        "Screening (V16, 2026-11-28)", "Strong | Kubernetes | 3 years", "Verdict: Skip (Seniority)",
        "Note: 6 years required, your limit is 4"]


def test_expired_and_closed_jobs_are_never_picked():
    rows = jobs()
    rows.append(("expired", {"Company": "Qualtrics", "Role": "Systems Engineer I",
                             "Screen verdict": "Apply high", "Status": "Expired",
                             "Skip reason": "Expired", "Swept date": SUNDAY.date()}))
    rows.append(("closed", {"Company": "EY", "Role": "DevOps", "Screen verdict": "Apply low",
                            "Status": "Declined", "Skip reason": "Expired",
                            "Swept date": SUNDAY.date() - timedelta(days=1)}))
    picked = {pid for pid, _ in sc.pick(rows, SUNDAY.date(), n=40)}
    assert "expired" not in picked and "closed" not in picked
    assert len(picked) == 20  # the other jobs of the week are all still picked


def test_the_why_of_a_skip_comes_from_the_page():
    values = {"Screen verdict": "Skip", "Status": "Declined", "Skip reason": "Other",
              "Ghost job risk": "Medium"}
    assert sc.why(values, BODY).splitlines() == [
        "Verdict: Skip (Seniority)", "Note: 6 years required, your limit is 4",
        "Status: Declined", "Skip reason: Other", "Ghost job risk: Medium"]
    assert sc.screening_lines(["Description source: x", "no screening yet"]) == []
    newest = ["Screening (V15, 2026-11-01)", "Verdict: Apply high", *BODY]
    assert sc.screening_lines(newest)[0] == "Verdict: Skip (Seniority)"  # the newest one
    assert sc.why({"Screen verdict": "Apply high", "Status": "Screened"}) == (
        "No reason saved on the job (only the verdict).")


def test_the_check_reads_each_picked_page():
    table = FakeTable()
    values = {"Company": "Co", "Role": "DevOps Engineer", "Screen verdict": "Skip",
              "Swept date": SUNDAY.date()}
    read = []

    def body(page_id):
        read.append(page_id)
        return BODY

    sc.run(table, None, None, [("p1", values)], SUNDAY, body=body)
    assert read == ["p1"]
    assert table.created[0]["Why"].startswith("Verdict: Skip (Seniority)\nNote: 6 years")

    def broken(page_id):
        raise RuntimeError("Notion is down")

    sc.run(FakeTable(), None, None, [("p1", values)], SUNDAY, body=broken)  # still a row


def test_the_desk_passes_its_page_reader(monkeypatch):
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.screen_check_table = FakeTable()
    got = {}
    monkeypatch.setattr(sc, "run", lambda *a, **kw: got.update(kw) or "ok")
    d.screen_check()
    assert got["body"] == d.repo.read_body
