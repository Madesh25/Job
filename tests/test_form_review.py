"""Auto-apply step 3 (Autopilot H8, 25 Oct): Answers OK and Something is wrong under a dry
run; the Answer bank and Auto-apply issues tables; screenshots kept 3 days after approval."""

from datetime import datetime, timedelta, timezone

from test_telegram_bot import Clock, settings

from jobengine.apply import answers, form_bot, form_review
from jobengine.apply.form_bot import Answer, Field, Result
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY

NOW = datetime(2026, 10, 1, 22, 10, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def _result(shot):
    return Result(url="https://job-boards.greenhouse.io/northwind/jobs/1", screenshot=shot,
                  answers=[Answer(Field("0", "text", "First name"), "Madeshwaran",
                                  source="profile"),
                           Answer(Field("1", "textarea", "Why Kubernetes?"),
                                  "I run it every day at Xerago.", source="ai"),
                           Answer(Field("2", "text", "Years of Terraform?"), "5 years",
                                  source="ai")])


def make(tmp_path, monkeypatch):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG")
    monkeypatch.setattr(form_bot, "dry_run", lambda *a, **kw: _result(shot))
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: NOW
    return d, shot


def test_answers_ok_saves_the_ai_answers_as_yours(tmp_path, monkeypatch):
    d, shot = make(tmp_path, monkeypatch)
    _, report = d.dryrun_command("https://job-boards.greenhouse.io/northwind/jobs/1")
    assert [label for label, _ in report.buttons] == ["✅ Answers OK", "❌ Something is wrong"]
    run_id = report.buttons[0][1].split(":")[1]
    [ok] = d.tap(f"fo:{run_id}")
    assert ok.text.startswith("✅ Approved:") and "2 AI answer(s) saved as yours" in ok.text
    assert {r["q"] for r in answers.items(d.state)} == {"Why Kubernetes?", "Years of Terraform?"}
    assert [r["Source"] for r in d.answer_bank.created] == ["AI approved", "AI approved"]
    assert d.tap(f"fo:{run_id}")[0].text == "Already approved."
    assert form_review.approved_count(d.state) == 1
    # the screenshot goes 3 days after the approval
    assert form_review.prune_approved(d.state, NOW + timedelta(days=2)) == 0
    assert form_review.prune_approved(d.state, NOW + timedelta(days=3, minutes=1)) == 1
    assert not shot.exists()


def test_something_is_wrong_saves_the_right_answer_and_an_issue(tmp_path, monkeypatch):
    d, _shot = make(tmp_path, monkeypatch)
    _, report = d.dryrun_command("https://job-boards.greenhouse.io/northwind/jobs/1")
    run_id = report.buttons[1][1].split(":")[1]
    [ask] = d.tap(f"fw:{run_id}")
    assert "3. Years of Terraform?: 5 years" in ask.text and f"Ref DRY-{run_id}" in ask.text
    [noted] = d.dryrun_note(ask.text, "3 = 3 years in production")
    assert noted.text.startswith("Noted (Wrong answer). Saved as your answer to "
                                 "\"Years of Terraform?\"")
    assert {"q": "Years of Terraform?", "a": "3 years in production"} in answers.items(d.state)
    [issue] = d.autoapply_issues.created
    assert issue["Form type"] == "Greenhouse" and issue["Status"] == "To fix"
    assert (issue["Bot answer"], issue["Correct answer"]) == ("5 years", "3 years in production")
    [missed] = d.dryrun_note(ask.text, "it missed the visa question")
    assert missed.text.startswith("Noted (Missed field).")
    assert d.dryrun_note("some other message", "3 = x") is None


def test_the_answer_bank_feeds_the_next_dry_run(tmp_path, monkeypatch):
    d, _shot = make(tmp_path, monkeypatch)
    d.answer_bank.created.append({"Question": "Driving licence?", "Answer": "Yes, category B",
                                  "Approved": True})
    got = {}

    def run(s, url, values, saved, **kw):
        got["saved"] = saved
        return Result(url=url, stopped="captcha")

    monkeypatch.setattr(form_bot, "dry_run", run)
    [report] = d.dryrun_command("https://jobs.lever.co/northwind/1")
    assert {"q": "Driving licence?", "a": "Yes, category B"} in got["saved"]
    assert report.buttons == []  # a stopped run has nothing to approve


def test_form_type_and_old_runs_are_dropped():
    assert form_review.form_type("https://jobs.ashbyhq.com/x") == "Ashby"
    assert form_review.form_type("https://careers.example.com/apply") == "Other"
    from jobengine.bot_state import FakeBotState

    state = FakeBotState()
    for _ in range(form_review.MAX_RUNS + 5):
        form_review.record(state, job="j", job_id="", url="u", shot=None, answers=[], now=NOW)
    assert len(state.get(form_review.KEY)["items"]) == form_review.MAX_RUNS
