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


@browser
def test_the_approved_resume_is_attached(tmp_path, monkeypatch):
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    seen = {}

    def check(page):
        seen["file"] = page.evaluate("() => cv.files.length ? cv.files[0].name : ''")

    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", VALUES, SAVED,
                              folder=tmp_path, prepare=_serve(FORM), check=check,
                              resume=("Madeshwaran_DevOps.pdf", b"%PDF-1.4 x"))
    assert seen["file"] == "Madeshwaran_DevOps.pdf"
    [cv] = [a for a in result.answers if a.field.kind == "file"]
    assert (cv.value, cv.source) == ("Madeshwaran_DevOps.pdf", "resume")
    assert not (tmp_path / "upload").exists() or not any((tmp_path / "upload").iterdir())


JOB_PAGE = """<!doctype html><html><head><title>DevOps Engineer - Northwind</title></head><body>
<h1>DevOps Engineer</h1><p>Run our cloud.</p>
<input type="file" name="hidden_cv" style="display:none">
<form><label for="alert">Email address without domain</label><input id="alert" type="email">
  <button>Get job alerts</button></form>
<a href="https://jobs.example.com/apply">Apply for this job</a></body></html>"""
TAB_PAGE = """<!doctype html><html><head><title>DevOps Engineer @ Northwind</title></head><body>
<nav class="tabs"><a href="https://jobs.example.com/job/1">Overview</a>
<a href="https://jobs.example.com/apply">Application</a></nav><p>Run our cloud.</p></body></html>"""


def _serve_pages(pages):
    def page(html):
        return lambda route: route.fulfill(status=200, body=html, content_type="text/html")

    def prepare(context):
        for url, html in pages.items():
            context.route(url, page(html))
    return prepare


@browser
@pytest.mark.parametrize("page", [JOB_PAGE, TAB_PAGE])
def test_the_job_page_is_not_taken_for_the_form(tmp_path, monkeypatch, page):
    """16 Nov, real forms: Recruitee's job page has a hidden upload field and Teamtailor's a
    job-alert email box; both were taken for the application form, so the bot never opened
    the real one. Ashby links its form from an "Application" tab."""
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    result = form_bot.dry_run(settings(), "https://jobs.example.com/job/1", VALUES, SAVED,
                              folder=tmp_path,
                              prepare=_serve_pages({"https://jobs.example.com/job/1": page,
                                                    "https://jobs.example.com/apply": FORM}))
    assert not result.stopped, result.stopped
    assert result.opened == "https://jobs.example.com/apply"
    assert result.title == "Apply: DevOps Engineer"


def test_what_counts_as_an_application_form():
    alert = [Field("0", "email", "Email address without domain")]
    assert not form_bot.application_form(alert)
    assert not form_bot.application_form([Field("0", "file", "CV or resume *")])
    assert form_bot.application_form([Field("0", "text", "First name")])
    assert form_bot.application_form([Field("0", "email", "Email"),
                                      Field("1", "text", "LinkedIn profile")])
    assert not form_bot.application_form([Field("0", "email", "Email"),
                                          Field("1", "file", "CV")])  # a job page's hidden one


def test_known_job_boards_give_their_form_page():
    """16 Nov, real forms: Recruitee and Teamtailor open the form with a button the bot never
    presses; the form has its own address."""
    page = form_bot.form_page
    assert page("https://intelic.recruitee.com/o/devops-engineer") == \
        "https://intelic.recruitee.com/o/devops-engineer/c/new"
    assert page("https://zinkworks.teamtailor.com/jobs/7774951-devops-engineer") == \
        "https://zinkworks.teamtailor.com/jobs/7774951-devops-engineer/applications/new"
    ashby = "https://jobs.ashbyhq.com/rentman/b796c722-4f18-4df0-807e-0dfe91f28a99"
    assert page(ashby) == ashby + "/application"
    assert page(ashby + "/application") is None  # already the form
    assert page("https://apply.workable.com/keylane/j/8DFC2B6474/") == \
        "https://apply.workable.com/keylane/j/8DFC2B6474/apply/"
    assert page("https://jobs.lever.co/acme/0b5d1d8e-1c2b-4a1e-9d3a-1234567890ab") == \
        "https://jobs.lever.co/acme/0b5d1d8e-1c2b-4a1e-9d3a-1234567890ab/apply"
    assert page("https://www.example.com/careers/devops") is None


def test_only_named_read_requests_pass_the_guard():
    """16 Nov: Ashby builds its form from two POSTs that only read the job; every other POST
    (an autosave, the application itself) stays blocked in a dry run."""
    from types import SimpleNamespace

    blocked, passed = [], []
    guard = form_bot._guard(settings(), blocked)

    def route(method, url):
        return SimpleNamespace(request=SimpleNamespace(method=method, url=url),
                               continue_=lambda: passed.append(url), abort=lambda: None)

    read = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
    send = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction"
    guard(route("POST", read))
    guard(route("POST", send))
    guard(route("POST", "https://jobs.ashbyhq.com/api/non-user-graphql"))
    assert passed == [read]
    assert [b.split(" ", 1)[1] for b in blocked] == [send,
                                                     "https://jobs.ashbyhq.com/api/non-user-graphql"]


NAMED_FORM = """<!doctype html><html><head><title>Apply</title></head><body><form>
  <label for="a">First</label><input id="a" name="first_name">
  <label for="b">Last</label><input id="b" name="last_name">
  <label for="c">Email* (required)</label><input id="c" name="email" type="email">
  <label for="d">Are you based in Amsterdam?* (required)</label>
  <select id="d" name="custom_1"><option>Please select</option><option>Yes</option>
    <option>No</option></select>
  <div class="label" id="up">Resume/CV</div><button type="button">Attach</button>
  <input id="resume" name="resume" type="file" aria-label="Attach">
  <button type="submit">Submit application</button></form></body></html>"""


@browser
def test_field_names_and_required_marks_in_the_text_are_read(tmp_path, monkeypatch):
    """16 Nov, real forms: Personio labels "First" and "Last" (names first_name, last_name)
    and marks required questions only in the text ("Email* (required)"); Greenhouse's resume
    field says "Attach" (name resume). The resume was never attached there and the gate saw
    no missing required question."""
    from jobengine.apply import form_submit

    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", VALUES, SAVED,
                              folder=tmp_path, prepare=_serve(NAMED_FORM),
                              resume=("Madeshwaran_DevOps.pdf", b"%PDF-1.4 x"))
    got = {a.field.label: a for a in result.answers}
    assert got["First"].value == VALUES["first_name"] and got["Last"].value
    assert got["Email* (required)"].field.required
    assert got["Attach"].source == "resume"
    gate = form_submit.blockers(result.answers, allowed_privacy=False)
    assert gate == ['required: "Are you based in Amsterdam?* (required)" (no answer yet)']


@pytest.mark.parametrize("label,key", [
    ("Do you have any connections within Lepaya? if so, share their full name.", None),
    ("Full name", "full_name"),
    ("Are you authorized to work in Ireland without visa sponsorship?",
     "authorized_without_sponsorship"),
    ("Will you now or in the future require visa sponsorship?", "sponsorship_needed"),
    ("Are you legally authorized to work in Ireland, or will you require sponsorship?", None),
    ("Allowed to work?* (required)", "authorized_without_sponsorship"),
    ("Available from* (required)", "earliest_start"),
    ("Where are you currently located?", "city"),
    ("Motivate why you are interested in this particular role", "why"),
    ("Data Protection Information Notice*", None),
    ("What is your current notice period?", "notice_period"),
])
def test_the_question_rules_found_on_real_forms(label, key):
    """16 Nov: the same corrections as the bookmark (apply/autofill.py, #330 and #333)."""
    values = {k: k for k in ("full_name", "authorized_without_sponsorship", "sponsorship_needed",
                             "earliest_start", "city", "why", "notice_period")}
    kind = "textarea" if key == "why" else "text"
    assert form_bot.rule_for(label, values, kind) == key


def test_prefer_not_to_answer_is_prefer_not_to_say():
    assert form_bot.choice(["Male", "Female", "Prefer not to answer"], "Prefer not to say") == 2


DROPDOWN_FORM = """<!doctype html><html><head><title>Apply</title></head><body><form>
<label for="fn">First Name*</label><input id="fn" name="first_name" required>
<div id="boxes"></div>
<button type="submit">Submit application</button></form>
<script>
/* Like Greenhouse's dropdowns (16 Nov): the list opens on key-up only, its options have ids
   react-select-<input id>-option-N, the choice shows in a single-value box and typed text is
   cleared when the box loses focus. */
function combo(id, label, options, chosen) {
  const box = document.createElement('div');
  box.innerHTML = `<label id="${id}-l" for="${id}">${label}</label><div class="wrap">
    <div class="select__control"><div class="select__value-container">
    <div class="select__single-value" id="${id}-v">${chosen || ''}</div>
    <div class="select__input-container"><input id="${id}" role="combobox"
      aria-autocomplete="list" aria-labelledby="${id}-l" aria-required="true"></div></div></div>
    <div class="menu"></div></div>`;
  document.getElementById('boxes').appendChild(box);
  const input = box.querySelector('input'), menu = box.querySelector('.menu');
  let open = false;
  const draw = () => {
    const typed = input.value.toLowerCase();
    menu.innerHTML = (open ? options.filter(o => o.toLowerCase().includes(typed)) : [])
      .map((o, i) => `<div role="option" id="react-select-${id}-option-${i}">${o}</div>`).join('');
    for (const o of menu.querySelectorAll('[role=option]')) {
      o.addEventListener('click', () => { box.querySelector('.select__single-value')
        .textContent = o.textContent; input.value = ''; open = false; draw(); });
    }
  };
  input.addEventListener('input', draw);
  box.querySelector('.wrap').addEventListener('keyup', () => { open = true; draw(); });
  input.addEventListener('blur', () => setTimeout(() => { input.value = ''; open = false;
    draw(); }, 100));
}
combo('q1', 'Will you require visa sponsorship?*', ['Yes', 'No']);
combo('q2', 'Country*', ['+48', '+91'], '+91');
combo('q3', 'Gender*', ['Female', 'Male', "I don't wish to answer"]);
combo('q4', 'Dial code*', ['+48', '+91']);
document.getElementById('fn').addEventListener('input', () => {  /* set by the form itself */
  document.getElementById('q4-v').textContent = '+91'; });
</script></body></html>"""


@browser
def test_custom_dropdowns_get_an_option_or_count_as_unanswered(tmp_path, monkeypatch):
    """16 Nov, real Greenhouse forms: the bot typed the answer into a dropdown and reported it
    filled; the dropdown cleared it, so the gate counted answers that were not there."""
    from jobengine.apply import form_submit

    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    seen = {}

    def check(page):
        page.wait_for_timeout(300)
        seen.update(page.evaluate("""() => ({q1: document.getElementById('q1-v').textContent,
          q2: document.getElementById('q2-v').textContent,
          q3: document.getElementById('q3-v').textContent})"""))

    values = {**VALUES, "sponsorship_needed": "No", "gender": "Prefer not to say"}
    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", values, SAVED,
                              folder=tmp_path, prepare=_serve(DROPDOWN_FORM), check=check)
    assert not result.stopped, result.stopped
    assert seen == {"q1": "No", "q2": "+91", "q3": ""}
    got = {a.field.label: a for a in result.answers}
    assert got["Will you require visa sponsorship?*"].value == "No"
    assert got["Country*"].source == "form"  # it showed an answer: left as it is
    assert got["Gender*"].note == 'no option matches "Prefer not to say"'
    assert (got["Dial code*"].value, got["Dial code*"].source) == ("+91", "form")
    gate = form_submit.blockers(result.answers, allowed_privacy=False)
    assert gate == ['required: "Gender*" (no option matches "Prefer not to say")',
                    "the resume is not attached"]  # this test page has no resume field


STYLED_RADIOS = """<!doctype html><html><head><title>Apply</title><style>
.choice{position:relative;display:inline-block;width:80px;height:24px}
.choice input{position:absolute;width:1px;height:1px;margin:0}
.choice span{position:absolute;inset:0;background:#eee}</style></head><body><form>
<label for="fn">Full name</label><input id="fn" name="name">
<fieldset><legend>Are you legally authorised to work in the Netherlands?</legend>
<label class="choice"><input type="radio" name="auth" value="true"><span>Yes</span></label>
<label class="choice"><input type="radio" name="auth" value="false"><span>No</span></label>
</fieldset></form></body></html>"""


@browser
def test_radios_under_styled_boxes_are_ticked(tmp_path, monkeypatch):
    """16 Nov, real Recruitee form (Intelic): the radio buttons are 1 px under styled boxes;
    "the form did not take the answer" for the work permit question."""
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    seen = {}

    def check(page):
        seen["auth"] = page.evaluate(
            "() => (document.querySelector('input[name=auth]:checked') || {}).value || ''")

    values = {**VALUES, "authorized_without_sponsorship": "No"}
    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", values, SAVED,
                              folder=tmp_path, prepare=_serve(STYLED_RADIOS), check=check)
    [auth] = [a for a in result.answers if a.field.kind == "radio"]
    assert (auth.value, auth.note) == ("No", "")
    assert seen["auth"] == "false"


HIDDEN_RADIOS = """<!doctype html><html><head><title>Apply</title><style>
.r{position:absolute;left:-9999px}
</style></head><body><form>
<label for="fn">Full name</label><input id="fn" name="name">
<fieldset><legend>Are you legally authorised to work in the Netherlands?</legend>
<input class="r" type="radio" id="auth:R1:-0" name="auth" value="true">
<label for="auth:R1:-0">Yes</label>
<input class="r" type="radio" id="auth:R1:-1" name="auth" value="false">
<label for="auth:R1:-1">No</label>
</fieldset></form></body></html>"""


@browser
def test_radios_hidden_off_screen_are_ticked_by_their_label(tmp_path, monkeypatch):
    """16 Nov, /dryrun on the real Recruitee form (Intelic) with a resume: the tick failed with
    "Element is outside of the viewport". A radio button kept off screen and named by a label
    beside it gives the same error."""
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    seen = {}

    def check(page):
        seen["auth"] = page.evaluate(
            "() => (document.querySelector('input[name=auth]:checked') || {}).value || ''")

    values = {**VALUES, "authorized_without_sponsorship": "No"}
    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", values, SAVED,
                              folder=tmp_path, prepare=_serve(HIDDEN_RADIOS), check=check)
    [auth] = [a for a in result.answers if a.field.kind == "radio"]
    assert (auth.value, auth.note) == ("No", "")
    assert seen["auth"] == "false"


def test_a_choice_with_every_word_of_the_answer():
    """16 Nov, /dryrun test on real forms: "Chennai, India" never matched a location choice
    like "Chennai, Tamil Nadu, India"."""
    options = ["Chennai Central, Chennai", "Chennai, Tamil Nadu, India", "Indiana, US"]
    assert form_bot.choice(options, "Chennai, India") == 1
    assert form_bot.choice(["Yes", "No"], "Chennai, India") is None
    assert form_bot.choice(["Male", "Female"], "Female") == 1  # one word: exact only


JOB_LIST = """<!doctype html><html><head><title>Jobs at Catawiki</title></head><body>
<form action="/catawiki"><label for="s">Search</label><input id="s" name="q">
<label for="d">Department</label><select id="d"><option>All</option></select></form>
</body></html>"""


def test_a_closed_job_is_known_by_its_address():
    assert form_bot.closed("https://job-boards.greenhouse.io/catawiki/jobs/8346092",
                           "https://job-boards.greenhouse.io/catawiki?error=true")
    assert not form_bot.closed("https://job-boards.greenhouse.io/catawiki/jobs/8346092",
                               "https://job-boards.greenhouse.io/catawiki/jobs/8346092#app")
    assert not form_bot.closed("https://jobs.example.com/apply", "https://jobs.example.com/")
    uuid = "https://jobs.ashbyhq.com/rentman/b796c722-4f18-4df0-807e-0dfe91f28a99"
    assert not form_bot.closed(uuid, uuid + "/application")
    assert form_bot.closed(uuid, "https://jobs.ashbyhq.com/rentman")


@browser
def test_a_job_that_now_opens_the_job_list_is_reported_closed(tmp_path, monkeypatch):
    """16 Nov, /dryrun on real Greenhouse jobs (Rent The Runway, Catawiki): a closed job's
    link opens the company's job list; the report said "send the form's own link"."""
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)

    def answer(route):
        if route.request.url.endswith("/jobs/8346092"):  # as the board sends it on
            route.fulfill(status=200, content_type="text/html", body=(
                "<script>location.replace('https://jobs.example.com/catawiki?error=true')"
                "</script>"))
        else:
            route.fulfill(status=200, body=JOB_LIST, content_type="text/html")

    def prepare(context):
        context.route(lambda url: "/catawiki" in url, answer)

    result = form_bot.dry_run(settings(), "https://jobs.example.com/catawiki/jobs/8346092",
                              VALUES, SAVED, folder=tmp_path, prepare=prepare)
    assert result.stopped == ("the job looks closed: its link now opens "
                              "https://jobs.example.com/catawiki?error=true (not this job's "
                              "page). Nothing to fill.")


ASHBY_LIKE = """<!doctype html><html><head><title>Apply</title></head><body><form>
<div class="_fieldEntry_1"><label>Full name</label><div><input name="_systemfield_name"></div></div>
<div class="_fieldEntry_2"><label>Location</label><div><input placeholder="Start typing..."></div>
</div>
<div class="_fieldEntry_3"><label>Team size*</label><div><input required></div></div>
<div><p>Autofill from resume</p><p>Upload your resume here</p><input type="file"></div>
</form></body></html>"""


@browser
def test_question_names_come_from_the_fields_box(tmp_path, monkeypatch):
    """16 Nov, /dryrun test on real Ashby forms: "Location" is the label of the field's box and
    the field only says "Start typing...", so the report asked "/answer Start typing... = ";
    the "Autofill from resume" upload had no name at all."""
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    values = {**VALUES, "full_name": "Madeshwaran Example", "city": "Chennai, India"}
    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", values, SAVED,
                              folder=tmp_path, prepare=_serve(ASHBY_LIKE))
    labels = [a.field.label for a in result.answers]
    assert labels == ["Full name", "Location", "Team size*", "Autofill from resume"]
    text = form_bot.report(result)
    assert "/answer Team size = <your answer>" in text


def test_the_answer_hint_has_no_required_marks():
    assert form_bot.question("School*") == "School"
    assert form_bot.question("Email* (required)") == "Email"
    assert form_bot.question("Phone*Required") == "Phone"
    assert form_bot.question("What is your current address?") == "What is your current address?"


def test_the_resume_goes_to_the_resume_field_not_the_autofill_upload():
    """16 Nov, /dryrun test on real Ashby forms (Nord Security): the "Autofill from resume"
    upload, which reads a CV to fill the form, came first and got the resume."""
    fields = [Field("0", "file", "Autofill from resume"), Field("1", "text", "Full name"),
              Field("2", "file", "Resume", required=True)]
    answers = form_bot.plan(fields, VALUES, SAVED, resume="cv.pdf")
    assert [(a.field.label, a.source) for a in answers if a.field.kind == "file"] == [
        ("Autofill from resume", ""), ("Resume", "resume")]
    only = form_bot.plan([Field("0", "file", "Autofill from resume")], VALUES, SAVED,
                         resume="cv.pdf")
    assert only[0].source == "resume"  # the form has no other place for it
