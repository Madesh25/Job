"""3 Nov, your request: auto-submit per site type. Besides the 10 approved dry runs overall,
a form is submitted by the bot only when its own site type (Workday, Greenhouse ... or the
company's own host for a custom form) has 3 approved dry runs (Config
apply.approvals_per_site)."""

from datetime import datetime

from test_form_submit import desk_with_result
from test_telegram_bot import settings

from jobengine.apply import form_review, queue
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore

NOW = datetime(2026, 11, 3, 10, 0)
ON = ConfigStore.from_values({"apply.submit": "true"})
PROD = settings("prod", DRY_RUN="false")
WORKDAY = "https://acme.wd3.myworkdayjobs.com/en-US/careers/job/Krakow/DevOps_R123/apply"


def test_site_keys():
    assert form_review.site_key(WORKDAY) == "Workday"
    assert form_review.site_key("https://boards.greenhouse.io/acme/jobs/1") == "Greenhouse"
    assert form_review.site_key("https://www.careers.acme.com/apply/7") == "careers.acme.com"


def approve(state, url, n):
    for i in range(n):
        run = form_review.record(state, job=f"Job {i}", job_id=str(i), url=url, shot=None,
                                 answers=[], now=NOW)
        form_review.approve(state, run, NOW)


def test_each_site_type_needs_its_own_approved_runs():
    state = FakeBotState()
    approve(state, WORKDAY, 2)
    approve(state, "https://boards.greenhouse.io/acme/jobs/1", 8)
    assert form_review.approved_for(state, "Workday") == 2
    assert form_review.approved_for(state, "Greenhouse") == 8
    assert queue.submit_block(PROD, ON, 10, 0, 0, "Workday", 2) == (
        "Workday forms: 2 of 3 dry runs approved (each site type needs its own)")
    assert queue.submit_block(PROD, ON, 10, 0, 0, "Greenhouse", 8) is None
    assert queue.submit_block(PROD, ON, 10, 0, 0, "Workday", 1) is not None
    loose = ConfigStore.from_values({"apply.submit": "true", "apply.approvals_per_site": "1"})
    assert queue.submit_block(PROD, loose, 10, 0, 0, "Workday", 2) is None
    assert queue.site_line(ON, form_review.approved_by_site(state)) == (
        "Per site type: Greenhouse 8/3 ✅, Workday 2/3.")
    assert queue.site_line(ON, {}).startswith("Per site type: none approved yet")


def test_the_count_lasts_when_old_runs_roll_off():
    state = FakeBotState()
    approve(state, WORKDAY, 3)
    approve(state, "https://boards.greenhouse.io/acme/jobs/1", form_review.MAX_RUNS)
    assert all(r["form"] == "Greenhouse" for r in state.get(form_review.KEY)["items"])
    assert form_review.approved_for(state, "Workday") == 3


def test_the_desk_passes_the_forms_own_site_type(monkeypatch):
    d, job_id, seen = desk_with_result(monkeypatch, "")
    calls = []
    monkeypatch.setattr(queue, "submit_block", lambda *a: calls.append(a) or "closed")
    d.dry_run_job(job_id, form_url=WORKDAY, submit=True)
    assert calls[0][5:] == ("Workday", 0)
    assert seen["finish"] is None  # closed: a dry run that comes to you
