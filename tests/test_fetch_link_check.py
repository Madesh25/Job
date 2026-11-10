"""10 Nov, your request: closed jobs leave the list in every /fetch, not only once a week."""

from datetime import date

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.screen.runner import job_row
from jobengine.settings import load_settings
from jobengine.sweep.models import SweepSummary
from jobengine.track import expired_check

TODAY = date(2026, 11, 10)
CFG = {**expired_check.DEFAULTS, "workers": 1}


def row(pid, url, status="Screened", verdict="Apply", posted=date(2026, 11, 1)):
    return job_row(pid, {"Company": pid.title(), "Role": "DevOps Engineer", "URL": url,
                         "Status": status, "Screen verdict": verdict, "Posted date": posted})


def pages(gone=()):
    opened = []

    def get(url):
        opened.append(url)
        if url in gone:
            raise http.HttpError("GET failed: HTTP 404", status=404)
        return url, "<html><body>DevOps Engineer. Apply now.</body></html>"
    return get, opened


def test_a_closed_job_is_expired_in_the_fetch_and_leaves_pending():
    get, opened = pages(gone={"https://acme.example/jobs/1"})
    updates = {}
    rows = [row("acme", "https://acme.example/jobs/1"), row("open", "https://open.example/2")]
    result = expired_check.after_fetch(rows, get, TODAY, FakeBotState(), CFG,
                                       lambda pid, props: updates.update({pid: props}))
    assert [r.page_id for r, _why in result.closed] == ["acme"]
    assert updates["acme"]["Status"] == "Expired" and updates["acme"]["Skip reason"] == "Expired"
    assert "open" not in updates and result.checked == 2 and len(opened) == 2


def test_linkedin_jobs_close_after_30_days_without_opening_linkedin():
    get, opened = pages()
    updates = {}
    rows = [row("old", "https://www.linkedin.com/jobs/view/1", posted=date(2026, 10, 1)),
            row("young", "https://www.linkedin.com/jobs/view/2", posted=date(2026, 10, 25))]
    result = expired_check.after_fetch(rows, get, TODAY, FakeBotState(), CFG,
                                       lambda pid, props: updates.update({pid: props}))
    assert list(updates) == ["old"]
    assert "a LinkedIn job posted 40 days ago" in result.closed[0][1]
    assert opened == [] and result.not_opened == 1


def test_pending_jobs_first_a_limit_per_fetch_and_a_recheck_after_3_days():
    get, opened = pages()
    state = FakeBotState()
    rows = [row("new", "https://new.example/1", status="New", verdict="Unscreened"),
            row("pending", "https://pending.example/1"),
            row("approved", "https://approved.example/1", status="Approved")]
    cfg = {**CFG, "max_links": 2}
    first = expired_check.after_fetch(rows, get, TODAY, state, cfg, lambda *a: None)
    assert opened == ["https://pending.example/1", "https://approved.example/1"]
    assert first.left == 1
    expired_check.after_fetch(rows, get, TODAY, state, cfg, lambda *a: None)
    assert opened[2:] == ["https://new.example/1"]  # the two checked wait 3 days
    later = date(2026, 11, 14)
    opened.clear()
    expired_check.after_fetch(rows, get, later, state, cfg, lambda *a: None)
    assert opened == ["https://pending.example/1", "https://approved.example/1"]


def test_the_fetch_summary_lists_the_closed_jobs():
    text = SweepSummary(closed_jobs=["Acme, DevOps Engineer: the job page answers HTTP 404 "
                                     "(not found)"], closed_checked=120).friendly_text()
    assert "Closed jobs taken off your list (Status Expired): 1 (120 job links checked)" in text
    assert "- Acme, DevOps Engineer: the job page answers HTTP 404" in text


def test_settings():
    cfg = expired_check.fetch_settings(load_settings("local", {}).sweep)
    assert (cfg["enabled"], cfg["max_links"], cfg["recheck_days"], cfg["linkedin_days"]) == (
        True, 150, 3, 30)


def test_the_fetch_runs_the_check_and_says_so():
    from jobengine.sweep.runner import fake_deps, run_sweep

    s = load_settings("local", {})
    deps = fake_deps(s)
    opened = []
    deps.closed_page = lambda url: opened.append(url) or (_ for _ in ()).throw(
        http.HttpError("GET failed: HTTP 404", status=404))
    summary = run_sweep(s, deps, date(2026, 10, 1))
    expired = [v for v in deps.repo.rows.values() if v.get("Status") == "Expired"]
    assert opened and len(expired) == len(summary.closed_jobs) == summary.closed_checked
    assert "Closed jobs taken off your list" in summary.friendly_text()
