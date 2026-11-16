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
    "first_name": "Test", "last_name": "Person", "email": "test@example.com",
    "phone": "+48 000 000 000", "linkedin": "https://www.linkedin.com/in/test", "city": "Dublin",
    "expected_salary": "EUR 60000", "notice_period": "1 month", "sponsorship_needed": "No",
    "authorized_without_sponsorship": "Yes", "cover_letter": "Dear team, ..."},
    "answers": [["Years of experience with Kubernetes", "4"]]}


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
    assert s["q2-value"] == "No, I chose this"  # a dropdown you answered stays yours
    assert s["q4-value"] == ""  # "1 month" matches no option: nothing chosen, typing cleared
    alert = result["dialogs"][-1]["message"]
    assert "1 required list question(s) still to choose" in alert
    assert "choose yourself: what is your notice period" in alert
