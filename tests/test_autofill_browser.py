"""The autofill bookmark run in a real browser (Chromium through Playwright for Node) on a test
form: plain fields, a radio group, a select, a consent box and the custom dropdowns of
Greenhouse's new forms (16 Nov). Skipped where node or its playwright package is missing (CI)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jobengine.apply import autofill

HERE = Path(__file__).parent / "autofill"
DATA = {"job": "Demo Co, DevOps Engineer", "fields": {
    "first_name": "Test", "last_name": "Person", "full_name": "Test Person",
    "email": "test@example.com",
    "phone": "+48 000 000 000", "linkedin": "https://www.linkedin.com/in/test", "city": "Dublin",
    "expected_salary": "EUR 60000", "notice_period": "1 month", "sponsorship_needed": "No",
    "authorized_without_sponsorship": "Yes", "cover_letter": "Dear team, ...",
    "earliest_start": "1 month after an offer", "gender": "Prefer not to say",
    "why": "Your platform team works on what I do best.", "other_diversity": "Prefer not to say",
    "relocation": "Open to relocate at my own cost", "education": "Mechanical Engineering"},
    "answers": [["Years of experience with Kubernetes", "4"],
                ["Where do you live", "Chennai, India"], ["Degree", "B.E."]]}


def _browser() -> bool:
    if shutil.which("node") is None:
        return False
    found = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True,
                           cwd=HERE)
    return found.returncode == 0


needs_browser = pytest.mark.skipif(not _browser(), reason="node with playwright not installed")


def run(tmp_path, data=DATA):
    mark, code = tmp_path / "mark.txt", tmp_path / "code.txt"
    mark.write_text(autofill.bookmarklet(), encoding="utf-8")
    code.write_text(autofill.encode(data), encoding="utf-8")
    out = subprocess.run(["node", str(HERE / "run.js"), str(HERE / "form.html"), str(mark),
                          str(code)], capture_output=True, text=True, timeout=60, check=True)
    result = json.loads(out.stdout)
    assert result["errors"] == []
    return result


@needs_browser
def test_the_bookmark_fills_a_form_and_never_submits(tmp_path):
    result = run(tmp_path)
    s = result["state"]
    assert (s["fn"], s["ln"], s["em"], s["sal"], s["yrs"]) == (
        "Test", "Person", "test@example.com", "EUR 60000", "4")
    assert s["cur"] == ""  # 16 Nov (Qube): a currency question is not the salary amount
    assert s["yl"] == "Dublin"  # 16 Nov (Ashby, Personio): "Your Location* (required)"
    assert (s["deg"], s["trv"]) == ("B.E.", "")  # 17 Nov: "Degree" only for a short label
    assert s["loc"] == "Typed by me"  # a field you filled stays yours
    assert s["spon"] == "No" and s["auth:Yes"] is True
    assert s["consent"] is False and not s["SUBMITTED"] and not s["CLICKED"]


@needs_browser
def test_custom_dropdowns_get_their_option_or_are_named(tmp_path):
    """16 Nov: on a Greenhouse-like dropdown the bookmark typed "No", marked it orange and
    counted it filled; the dropdown cleared the text on blur, so the answer was lost."""
    result = run(tmp_path)
    s = result["state"]
    assert s["q1-value"] == "Yes"  # the option was chosen from the dropdown's list
    assert s["q3-value"] == "Dublin, County Dublin, Ireland"  # "Dublin" picks the first match
    # 16 Nov: "Chennai, India" picks the choice with both words
    assert s["q7-value"] == "Chennai, Tamil Nadu, India"
    assert s["q2-value"] == "No, I chose this"  # a dropdown you answered stays yours
    assert s["q4-value"] == ""  # "1 month" matches no option: nothing chosen, typing cleared
    alert = result["dialogs"][-1]["message"]
    assert "2 required list question(s) still to choose" in alert  # q4, and q6 asks two things
    assert "choose yourself: what is your notice period" in alert


@needs_browser
def test_authorized_without_sponsorship_is_the_authorization_answer(tmp_path):
    """16 Nov: "Are you authorized to work in Ireland without visa sponsorship?" got the
    sponsorship-needed answer (the word "sponsor" was checked first). For someone who needs a
    visa that is "Yes" to a question whose true answer is "No"."""
    needs_visa = {**DATA, "fields": {**DATA["fields"], "sponsorship_needed": "Yes",
                                     "authorized_without_sponsorship": "No"}}
    result = run(tmp_path, needs_visa)
    s = result["state"]
    assert s["q5-value"] == "No"  # authorized without sponsorship: No
    assert s["spon"] == "Yes"  # "Will you require visa sponsorship?": Yes
    assert s["auth:No"] is True  # "Are you legally authorized to work in Ireland?": No
    assert s["q6-value"] == ""  # asks both at once: left for you
    # 16 Nov, UbiOps (Personio): no choice says plain "No" ("Not yet, I need a residence
    # permit ..."): nothing is chosen, and the message names the question.
    assert s["perm"] == "Please select"
    assert "allowed to work in ireland" in result["dialogs"][-1]["message"]


@needs_browser
def test_a_greenhouse_dropdown_opens_on_key_up_and_its_hidden_twin_is_not_counted(tmp_path):
    """16 Nov, real forms (BearingPoint, InterSystems, Focal Systems): Greenhouse opens its
    dropdowns only on key-up or mouse-up, so no option was ever picked; its hidden required
    twin boxes were counted as required fields still empty; a phone-country list on the page
    must never be used for another question."""
    result = run(tmp_path)
    s = result["state"]
    assert s["gh1-value"] == "No"  # sponsorship needed: No (the test answers)
    assert s["gh2-value"] == "Yes"  # chosen by you before: never changed (16 Nov, BearingPoint)
    alert = result["dialogs"][-1]["message"]
    assert "1 required field is still empty" in alert  # only "other", not the hidden twin


@needs_browser
def test_diversity_job_location_and_discipline_lists_get_their_choice(tmp_path):
    """17 Nov, real Greenhouse form (InterSystems): Hispanic/Latino, Veteran Status, Job
    Location and Discipline stayed empty; the list shows only options with the typed text, so
    "Prefer not to say" found nothing."""
    s = run(tmp_path)["state"]
    assert s["gh3-value"] == "Decline To Self Identify"
    assert s["gh4-value"] == "I don't wish to answer"
    assert s["gh5-value"] == "Able to relocate to job location"
    assert s["gh6-value"] == "Mechanical Engineering"


@needs_browser
def test_a_phone_with_only_its_country_code_and_number_boxes_are_filled(tmp_path):
    """16 Nov, real forms: Recruitee's phone box starts with "+31", so it counted as yours and
    stayed unfilled (Xebia, Intelic); Teamtailor's salary box takes only numbers, refused
    "EUR ..." and was still marked orange and counted (Zinkworks)."""
    data = {**DATA, "fields": {**DATA["fields"], "expected_salary": "EUR 60 000 - 70 000"}}
    result = run(tmp_path, data)
    s = result["state"]
    assert s["mob"] == "+48 000 000 000"  # "+31" alone is not your answer
    assert s["num"] == "60000"
    assert s["dt"] == ""  # a date box does not take "1 month after an offer"
    alert = result["dialogs"][-1]["message"]
    assert "did not take the answer, fill yourself: available to start" in alert


@needs_browser
def test_questions_seen_on_real_forms_get_the_right_answer_or_none(tmp_path):
    """16 Nov, real forms: Personio's "connections within Lepaya? share their full name" got
    your name; "Where are you currently located?" (Ashby), "Allowed to work?" and "Available
    from" (Personio) and "Motivate why you are interested" (Teamtailor) were missed;
    Greenhouse's "Data Protection Information Notice" was taken for the notice period;
    "Prefer not to answer" is "Prefer not to say" (Teamtailor)."""
    s = run(tmp_path)["state"]
    assert s["conn"] == ""  # someone else's name: never yours
    assert s["curloc"] == "Dublin"
    assert s["allowed"] == "Yes"  # authorized without sponsorship: Yes (the test answers)
    assert s["avail"] == "1 month after an offer"
    assert s["motiv"] == "Your platform team works on what I do best."
    assert s["dpn"] == "Select..."  # a privacy notice is not the notice period
    assert s["gender2:x"] is True
    assert s["perm"] == "Yes, I am an EU citizen"  # "Yes" is the start of that choice
