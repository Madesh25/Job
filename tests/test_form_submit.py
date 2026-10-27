"""Auto-submit (Autopilot H8 step 5, 27 Oct, your OK): a complete form is submitted only
behind the gate; anything not ready comes to you as Apply by hand. Made-up forms."""

from test_form_bot import FORM, SAVED, VALUES, _serve, browser
from test_telegram_bot import Clock, settings

from jobengine.apply import form_bot, form_submit, queue
from jobengine.apply.form_bot import Answer, Field, Result
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY

PRIVACY = Field("9", "checkbox", "I agree to the privacy policy", required=True)
NEWS = Field("8", "checkbox", "Send me marketing news", required=True)
CV = Field("7", "file", "Resume/CV")
NAME = Field("1", "text", "First name", required=True)
THANKS = "<html><body><h1>Thank you for applying!</h1></body></html>"


def answers(*items):
    return list(items)


def test_what_stops_a_submit():
    ok_name = Answer(NAME, value="Madeshwaran", source="profile")
    cv = Answer(CV, value="cv.pdf", source="resume")
    assert form_submit.blockers([ok_name, cv], False) == []
    assert form_submit.blockers([Answer(NAME, note="no answer yet"), cv], False) == [
        'required: "First name" (no answer yet)']
    assert form_submit.blockers([ok_name, Answer(CV, note="your resume: no approved resume")],
                                False) == ["the resume is not attached"]
    ai = Answer(Field("2", "textarea", "Why us?"), value="I like it", source="ai")
    assert form_submit.blockers([ok_name, cv, ai], False) == [
        'a new AI answer for "Why us?" (approve the dry run first)']
    consent = Answer(PRIVACY, note="a consent box: you tick it yourself")
    assert form_submit.blockers([ok_name, cv, consent], False) == [
        'a required consent box: "I agree to the privacy policy"']
    assert form_submit.blockers([ok_name, cv, consent], True) == []  # apply.tick_privacy
    news = Answer(NEWS, note="a consent box: you tick it yourself")
    assert form_submit.blockers([ok_name, cv, news], True) == [
        'a required consent box: "Send me marketing news"']  # never ticked


def test_the_gate():
    on = ConfigStore.from_values({"apply.submit": "true"})
    prod = settings("prod", DRY_RUN="false")
    assert form_submit.tick_privacy(ConfigStore.from_values({"apply.tick_privacy": "true"}))
    assert queue.submit_block(settings(), on, 10, 0, 0) == (
        "only the prod bot with DRY_RUN false submits")
    assert queue.submit_block(settings("prod"), on, 10, 0, 0) == (
        "only the prod bot with DRY_RUN false submits")  # DRY_RUN true by default
    assert queue.submit_block(prod, ConfigStore.from_values({}), 10, 0, 0) == (
        "Config apply.submit is not true")
    assert queue.submit_block(prod, on, 9, 0, 0) == "9 of 10 dry runs approved"
    assert queue.submit_block(prod, on, 12, 1, 0) == (
        "12 of 10 dry runs approved, 1 marked wrong in the last 10")
    assert queue.submit_block(prod, on, 10, 0, 50) == "today's 50 forms are submitted"
    assert queue.submit_block(prod, on, 10, 0, 49) is None
    assert queue.submit_status(on, 10, 0, prod, 3).startswith(
        "Auto-submit: on. 3 of 50 forms submitted today")


def _thanks(context):
    context.route("https://jobs.example.com/apply/submit",
                  lambda route: route.fulfill(status=200, body=THANKS, content_type="text/html"))


@browser
def test_a_complete_form_is_submitted_and_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    monkeypatch.setattr(form_submit, "CONFIRM_WAIT_MS", 5_000)
    serve = _serve(FORM)

    def prepare(context):
        serve(context)
        _thanks(context)

    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", VALUES, SAVED,
                              folder=tmp_path, prepare=prepare,
                              resume=("Madeshwaran_DevOps.pdf", b"%PDF-1.4 x"),
                              finish=form_submit.finisher(True))
    assert not result.stopped, result.stopped
    assert result.submitted == "confirmed"
    privacy = [a for a in result.answers if a.field.kind == "checkbox"]
    assert privacy[0].value == "ticked (privacy consent)"
    assert form_submit.report(result, "Northwind Cloud, DevOps Engineer").startswith(
        "✅ Applied by the bot: Northwind Cloud, DevOps Engineer.")


@browser
def test_without_the_privacy_switch_nothing_is_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    seen = {}

    def check(page):
        seen["sent"] = page.evaluate("() => !!window.sent")

    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", VALUES, SAVED,
                              folder=tmp_path, prepare=_serve(FORM), check=check,
                              resume=("cv.pdf", b"%PDF-1.4 x"),
                              finish=form_submit.finisher(False))
    assert result.submitted == ('not submitted: a required consent box: "I agree to the '
                                'privacy policy"')
    assert seen["sent"] is False
    assert form_submit.report(result, "Northwind").startswith(
        "📝 Apply by hand: Northwind\nhttps://jobs.example.com/apply\nWhy not by the bot: a "
        "required consent box")


def desk_with_result(monkeypatch, submitted):
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    job_id = next(iter(d.repo.rows))
    monkeypatch.setattr(queue, "submit_block", lambda *a: None)
    seen = {}

    def fake_run(s, url, values, saved, **kw):
        seen["finish"] = kw.get("finish")
        return Result(url=url, answers=[Answer(NAME, value="Madeshwaran", source="profile")],
                      submitted=submitted)

    monkeypatch.setattr(form_bot, "dry_run", fake_run)
    return d, job_id, seen


def test_a_confirmed_submit_marks_the_job_applied(monkeypatch):
    d, job_id, seen = desk_with_result(monkeypatch, "confirmed")
    replies = d.dry_run_job(job_id, submit=True)
    assert seen["finish"] is not None
    assert replies[0].text.startswith("✅ Applied by the bot:")
    assert d.repo.rows[job_id]["Status"] == "Applied"
    assert d.repo.rows[job_id]["Applied date"] == d.tracking_now().date()
    today = d.tracking_now().date().isoformat()
    assert queue.submitted_today(d.state, today) == 1


def test_a_form_not_ready_comes_to_you_with_the_dry_run(monkeypatch):
    d, job_id, seen = desk_with_result(monkeypatch, "not submitted: the resume is not attached")
    replies = d.dry_run_job(job_id, submit=True)
    assert replies[0].text.startswith("📝 Apply by hand:")
    assert replies[-1].buttons  # Answers OK / Something is wrong: the dry run review
    assert d.repo.rows[job_id]["Status"] != "Applied"


def test_dryrun_command_never_submits(monkeypatch):
    d, job_id, seen = desk_with_result(monkeypatch, "")
    d.dryrun_command(job_id)
    assert seen["finish"] is None
