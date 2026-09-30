"""One job at a time: resume review, apply, then the next job (30 Sep feedback)."""

import tempfile
from pathlib import Path

from test_telegram_bot import Clock, fake_rendering, settings

from jobengine.screen.desk import FETCH_HINT, NOT_APPLYING_ASK, ONE_AT_A_TIME, fake_desk
from jobengine.sweep.fakes import FAKE_TODAY


def make_desk():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    fake_rendering(d, Path(tempfile.mkdtemp()))
    d.screen()
    return d


def is_card(reply):
    return any(data.startswith("ap:") for _, data in reply.buttons)


def approve_resume(d, job="pl-clean"):
    d.tap(f"ap:{job}")
    log_id = d.resume.resume_log.all_rows()[0][0]
    return d.tap("ra:" + log_id.replace("-", ""))


def test_approve_shows_the_resume_and_not_the_next_job():
    d = make_desk()
    replies = d.tap("ap:pl-clean")
    assert replies[0].document is not None  # the resume PDF with Approve resume / Rebuild
    assert [label for label, _ in replies[0].buttons][:2] == ["Approve resume", "Rebuild"]
    assert replies[-1].text == ONE_AT_A_TIME
    assert not any(is_card(r) for r in replies)


def test_a_failed_build_moves_on_to_the_next_job():
    d = make_desk()
    d.resume.blocks = lambda: []  # no Golden Master: nothing to review
    replies = d.tap("ap:pl-clean")
    assert replies[0].document is None
    assert is_card(replies[-1])


def test_approve_resume_gives_the_link_then_i_applied_brings_the_next_job():
    d = make_desk()
    replies = approve_resume(d)
    last = replies[-1]
    assert last.text.startswith("Resume approved and saved. Apply here: https://")
    assert [data.split(":")[0] for _, data in last.buttons] == ["ia", "na"]
    assert last.text.endswith(FETCH_HINT)
    assert not any(is_card(r) for r in replies)
    after = d.tap("ia:pl-clean")
    assert after[0].text.startswith("Marked as applied on 2026-10-01")
    assert is_card(after[-1]) or after[-1].text.startswith("Nothing to review")
    assert d.repo.rows["pl-clean"]["Status"] == "Applied"


def test_not_applying_asks_why_and_records_it():
    d = make_desk()
    approve_resume(d)
    ask = d.tap("na:pl-clean")[0]
    assert ask.text == NOT_APPLYING_ASK
    assert [label for label, _ in ask.buttons] == [
        "the job is closed or expired", "not a fit after all", "another reason"]
    replies = d.tap(ask.buttons[0][1])
    assert replies[0].text == ("Marked as not applied: Vistula Cloud, DevOps Engineer (the job "
                               "is closed or expired).")
    row = d.repo.rows["pl-clean"]
    assert (row["Status"], row["Skip reason"]) == ("Declined", "Expired")
    assert any(b.startswith("Not applied (2026-10-01): the job is closed or expired")
               for b in d.repo.read_body("pl-clean"))
    assert is_card(replies[-1]) or replies[-1].text.startswith("Nothing to review")
    # A second tap changes nothing.
    again = d.tap(ask.buttons[1][1])[0].text
    assert again == "Already handled (Status is Declined)."


def test_skip_still_shows_the_next_job_at_once():
    d = make_desk()
    replies = d.tap("sk:pl-clean")
    assert replies[0].text.startswith("Skipped: ")
    assert is_card(replies[-1])
