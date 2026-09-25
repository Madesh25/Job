from datetime import datetime, timedelta

from jobengine.bot_state import FakeBotState
from jobengine.screen.jd_capture import (
    KEY,
    Captures,
    board_for_url,
    linkedin_posting_id,
    parse_jd_command,
    split_fields,
)

NOW = datetime(2026, 10, 1, 9, 0)
BOARDS = {"linkedin.com": "LinkedIn", "irishjobs.ie": "IrishJobs.ie"}


def test_linkedin_posting_id():
    assert linkedin_posting_id("https://www.linkedin.com/jobs/view/4012345678/") == (
        "linkedin:4012345678")
    assert linkedin_posting_id(
        "https://www.linkedin.com/jobs/view/devops-engineer-at-acme-4012345678") == (
        "linkedin:4012345678")
    assert linkedin_posting_id(
        "https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=devops") == (
        "linkedin:4012345678")
    assert linkedin_posting_id("https://jobs.example.com/4012345678") is None


def test_board_for_url():
    assert board_for_url("https://www.linkedin.com/jobs/view/1", BOARDS) == "LinkedIn"
    assert board_for_url("https://www.irishjobs.ie/job/1", BOARDS) == "IrishJobs.ie"
    assert board_for_url("https://jobs.example.com/1", BOARDS) == "Other"


def test_parse_jd_command_and_fields():
    assert parse_jd_command("https://x.example.com/1 Company: Acme\nRole: SRE\nText") == (
        "https://x.example.com/1", "Company: Acme\nRole: SRE\nText")
    assert parse_jd_command("") == (None, "")
    assert split_fields("Company: Acme\nrole:  SRE \nWe need Kubernetes.\nCity: not a field") == (
        {"company": "Acme", "role": "SRE"}, "We need Kubernetes.\nCity: not a field")


def test_capture_collects_and_survives_in_bot_state():
    state = FakeBotState()
    captures = Captures(state)
    capture = captures.start("https://x.example.com/1", NOW, "p1")
    captures.add(capture, "First part.")
    # A new Captures on the same store (a bot restart) sees the same capture.
    again = Captures(state).get()
    again = Captures(state).add(again, "Second part.")
    assert again.text() == "First part.\nSecond part."
    assert again.page_id == "p1"
    assert state.get(KEY)["chars"] == again.chars
    captures.clear()
    assert captures.get() is None


def test_capture_window():
    captures = Captures(FakeBotState(), window_minutes=20)
    capture = captures.start("https://x.example.com/1", NOW, None)
    assert not captures.expired(capture, NOW + timedelta(minutes=20))
    assert captures.expired(capture, NOW + timedelta(minutes=21))
