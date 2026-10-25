"""Auto-apply dry run (Autopilot H8, 25 Oct): the form is filled from your answers only,
consent boxes and files are left to you, nothing is ever sent, captcha and login stop it."""

import glob
import os
import time

import pytest
from test_telegram_bot import settings

from jobengine.apply import form_bot
from jobengine.apply.form_bot import Field, Result

VALUES = {"first_name": "Madeshwaran", "last_name": "Example", "email": "me@example.com",
          "phone": "+91 90000 00000", "linkedin": "https://www.linkedin.com/in/example",
          "sponsorship_needed": "Yes", "authorized_without_sponsorship": "No",
          "notice_period": "90 days", "why": "The role's focus on Kubernetes is close to my work."}
SAVED = [{"q": "Years of Terraform?", "a": "3 years in production"}]


def test_each_field_gets_your_answer_or_stays_empty():
    fields = [Field("0", "text", "First name", required=True),
              Field("1", "email", "Email address"),
              Field("2", "select", "Will you now or in the future require visa sponsorship?",
                    ("Select...", "Yes", "No")),
              Field("3", "radio", "Are you legally authorized to work in Poland?", ("Yes", "No")),
              Field("4", "text", "How many years of Terraform experience do you have?"),
              Field("5", "textarea", "Why do you want to join us?"),
              Field("6", "file", "Resume/CV", required=True),
              Field("7", "checkbox", "I agree to the privacy policy", required=True),
              Field("8", "text", "Do you hold a driving licence?"),
              Field("9", "text", "Company name of your referrer")]
    answers = {a.field.ref: a for a in form_bot.plan(fields, VALUES, SAVED)}
    assert answers["0"].value == "Madeshwaran" and answers["0"].source == "profile"
    assert answers["1"].value == "me@example.com"
    assert (answers["2"].value, answers["2"].option) == ("Yes", 1)
    assert (answers["3"].value, answers["3"].option) == ("No", 1)
    assert answers["4"].value == "3 years in production" and answers["4"].source == "saved"
    assert answers["5"].value.startswith("The role's focus")
    assert answers["6"].value is None and "resume" in answers["6"].note
    assert answers["7"].value is None and answers["7"].note.startswith("a consent box")
    assert answers["8"].value is None and answers["8"].note == "no answer yet"
    assert answers["9"].value is None  # "name" of someone else is not your name


def test_a_choice_with_no_matching_option_is_not_guessed():
    [answer] = form_bot.plan([Field("0", "select", "Notice period", ("1 month", "2 months"))],
                             VALUES, [])
    assert answer.option is None and answer.note == 'no option matches "90 days"'


def test_the_report_lists_what_was_filled_and_what_is_left():
    fields = [Field("0", "text", "First name"), Field("1", "text", "Years of Ansible?",
                                                      required=True)]
    result = Result(url="https://jobs.example.com/apply",
                    answers=form_bot.plan(fields, VALUES, []), blocked=["POST https://x"])
    text = form_bot.report(result, "Northwind Cloud, DevOps Engineer")
    assert text.splitlines()[0] == ("🧪 Dry run (Northwind Cloud, DevOps Engineer): nothing was "
                                    "sent.")
    assert "Filled 1 of 2 field(s)." in text
    assert "✅ First name: Madeshwaran (your profile and Apply pack)" in text
    assert "❗ Years of Ansible?: no answer yet" in text
    assert "/answer Years of Ansible? = <your answer>" in text
    assert "Blocked 1 request(s)" in text
    stopped = form_bot.report(Result(url="u", stopped="the page asks for a captcha."))
    assert stopped.endswith("Stopped: the page asks for a captcha.")


def test_old_screenshots_are_deleted(tmp_path):
    old, new = tmp_path / "old.png", tmp_path / "new.png"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    eight_days = time.time() - 8 * 86_400
    os.utime(old, (eight_days, eight_days))
    assert form_bot.prune(tmp_path) == 1
    assert not old.exists() and new.exists()


@pytest.mark.parametrize("url", ["https://www.linkedin.com/jobs/view/1",
                                 "http://jobs.example.com/apply", "https://10.0.0.1/apply"])
def test_linkedin_http_and_private_hosts_are_never_opened(url):
    result = form_bot.dry_run(settings(), url, VALUES, [])
    assert result.stopped.startswith("refusing to fetch") and result.screenshot is None


FORM = """<!doctype html><html><head><title>Apply: DevOps Engineer</title></head><body>
<form id="f" method="post" action="https://jobs.example.com/apply/submit">
  <label for="fn">First name *</label><input id="fn" name="first_name" required>
  <label for="ln">Last name</label><input id="ln" name="last_name">
  <label for="em">Email</label><input id="em" type="email" name="email">
  <label for="sp">Will you require visa sponsorship?</label>
  <select id="sp" name="sp"><option value="">Select...</option><option>Yes</option>
    <option>No</option></select>
  <fieldset><legend>Are you legally authorized to work in Poland?</legend>
    <label><input type="radio" name="auth" value="y">Yes</label>
    <label><input type="radio" name="auth" value="n">No</label></fieldset>
  <label for="tf">Years of Terraform?</label><input id="tf" name="tf">
  <label for="why">Why do you want to join us?</label><textarea id="why"></textarea>
  <label for="cv">Resume/CV</label><input id="cv" type="file">
  <label><input id="ok" type="checkbox" required> I agree to the privacy policy</label>
  <button type="submit">Submit application</button>
</form>
<script>
  document.getElementById('f').addEventListener('submit', () => { window.sent = true; });
  document.getElementById('fn').addEventListener('input', () => {
    fetch('https://jobs.example.com/autosave', {method: 'POST', body: 'x'}).catch(() => {});
  });
</script></body></html>"""
CAPTCHA = ("<html><body><label for=a>Email</label><input id=a>"
           "<div class='g-recaptcha'></div></body></html>")


def _browser_ready() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    if not os.environ.get(form_bot.CHROMIUM_ENV):
        found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
        if found:
            os.environ[form_bot.CHROMIUM_ENV] = found[-1]
    return True


browser = pytest.mark.skipif(not _browser_ready(), reason="Playwright is not installed")


def _serve(html):
    def prepare(context):
        context.route("https://jobs.example.com/apply",
                      lambda route: route.fulfill(status=200, body=html,
                                                  content_type="text/html"))
    return prepare


@browser
def test_the_browser_fills_the_form_and_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(form_bot, "SETTLE_MS", 200)
    seen = {}

    def check(page):
        seen.update(page.evaluate("""() => ({
          fn: fn.value, em: em.value, sp: sp.value, why: why.value, tf: tf.value,
          auth: (document.querySelector('input[name=auth]:checked') || {}).value || '',
          ok: ok.checked, sent: !!window.sent})"""))
        page.wait_for_timeout(300)  # the autosave the typing started

    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", VALUES, SAVED,
                              name="Northwind Cloud", folder=tmp_path,
                              prepare=_serve(FORM), check=check)
    assert not result.stopped, result.stopped
    assert result.title == "Apply: DevOps Engineer"
    assert seen == {"fn": "Madeshwaran", "em": "me@example.com", "sp": "Yes",
                    "why": VALUES["why"], "tf": "3 years in production", "auth": "n",
                    "ok": False, "sent": False}
    assert any(b.startswith("POST https://jobs.example.com/autosave") for b in result.blocked)
    assert result.screenshot is not None and result.screenshot.read_bytes()[:4] == b"\x89PNG"
    assert len(form_bot.filled(result.answers)) == 7


@browser
def test_a_captcha_stops_the_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", VALUES, [],
                              folder=tmp_path, prepare=_serve(CAPTCHA))
    assert result.stopped.startswith("the page asks for a captcha")
    assert result.answers == []


def test_dryrun_sends_the_screenshot_and_the_answers(tmp_path, monkeypatch):
    from test_telegram_bot import Clock

    from jobengine import telegram_bot as tb
    from jobengine.screen.desk import fake_desk
    from jobengine.sweep.fakes import FAKE_TODAY

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG")
    calls = []

    def fake_run(s, url, values, saved, **kw):
        calls.append((url, values))
        return Result(url=url, answers=form_bot.plan([Field("0", "text", "Notice period")],
                                                     {"notice_period": "90 days"}, []),
                      screenshot=shot)

    monkeypatch.setattr(form_bot, "dry_run", fake_run)
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    shot_reply, text_reply = d.dryrun_command("https://jobs.example.com/apply")
    assert calls[0][0] == "https://jobs.example.com/apply"
    assert shot_reply.document == ("shot.png", b"\x89PNG")
    assert "✅ Notice period: 90 days" in text_reply.text
    assert d.dryrun_command("")[0].text.startswith("Send /dryrun")
    assert "dryrun" in {c["command"] for c in tb.bot_commands()}
