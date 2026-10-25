"""Weekly spot check (Autopilot H5, 25 Oct): 2 of the week's resumes on Sunday evening."""

import random
from datetime import date, datetime, timedelta, timezone

from test_telegram_bot import Clock, settings

from jobengine import telegram_bot as tb
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import spot_check

IST = timezone(timedelta(hours=5, minutes=30))
SUNDAY = datetime(2026, 10, 4, 20, 5, tzinfo=IST)


def _row(job, day, revision=1, approved=True):
    return {"Job": [job], "Date": day.isoformat(), "Revision": revision, "Approved": approved,
            "Resume name": f"{job}.pdf", "File": "x"}


def test_pick_takes_the_week_approved_newest_revision_per_job():
    today = date(2026, 10, 4)
    rows = [("a1", _row("A", today, 1)), ("a2", _row("A", today, 2)),
            ("b1", _row("B", today - timedelta(days=3))),
            ("c1", _row("C", today - timedelta(days=9))),  # older than a week
            ("d1", _row("D", today, approved=False))]
    picked = spot_check.pick(rows, today, random.Random(1), n=5)
    assert sorted(log_id for log_id, _ in picked) == ["a2", "b1"]
    assert len(spot_check.pick(rows, today, random.Random(1))) == 2


def test_due_on_sunday_evening_once_a_week():
    from jobengine.bot_state import FakeBotState

    state = FakeBotState()
    assert spot_check.due(state, SUNDAY)
    assert not spot_check.due(state, SUNDAY.replace(hour=19))
    assert not spot_check.due(state, SUNDAY - timedelta(days=1))
    spot_check.mark_sent(state, SUNDAY.date())
    assert not spot_check.due(state, SUNDAY.replace(hour=21))
    assert spot_check.due(state, SUNDAY + timedelta(days=7))


def test_letter_is_read_from_the_job_page():
    body = ["Apply pack (2026-10-01)", "Cover letter (paste it if the form asks for one; only "
            "facts from your approved resume):\nDear team,\nI build Kubernetes platforms.",
            "Tips for this application"]
    assert spot_check.letter(body) == "Dear team,\nI build Kubernetes platforms."
    assert spot_check.letter(["nothing here"]) is None


def test_the_desk_sends_cards_and_keeps_notes(monkeypatch):
    from jobengine.mail import drafter

    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: SUNDAY
    job_id = next(iter(d.repo.rows))
    d.resume.resume_log.create(_row(job_id, SUNDAY.date()), [])
    monkeypatch.setattr(drafter, "resume_pdf", lambda deps, row: b"%PDF-1.4 fake")
    [card] = d.spot_check_tick()
    assert card.text.startswith("🔍 Spot check: ")
    assert card.document[1] == b"%PDF-1.4 fake"
    assert [b[1].split(":")[0] for b in card.buttons] == ["sf", "sw"]
    assert d.spot_check_tick() == []  # once a week
    ref = card.buttons[1][1].split(":")[1]
    [ask] = d.tap(f"sw:{ref}")
    assert ask.text.startswith("What is wrong with ") and f"Ref SPOT-{ref}" in ask.text
    [saved] = d.spot_note(ask.text, "says 5 years of Terraform, I have 3")
    assert saved.text.startswith("Saved (1 note(s))")
    notes = d.spot_check_command("notes")[0].text
    assert notes.endswith(": says 5 years of Terraform, I have 3")
    assert d.tap(f"sf:{ref}")[0].text.startswith("👍 Thanks")
    assert "spotcheck" in {c["command"] for c in tb.bot_commands()}


def test_no_resume_this_week():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: SUNDAY
    assert d.spot_check()[0].text == "🔍 Spot check: no resume was approved in the last 7 days."
