"""/autopilot: fetch, half-price screening, approvals, resumes, contacts and drafts."""

from datetime import datetime

import pytest
from test_telegram_bot import ButtonTelegram, Clock, fake_rendering, settings, update

from jobengine import telegram_bot as tb
from jobengine.resume.builder import BUILDABLE_STATUSES
from jobengine.screen import autopilot, batch
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY


def make_desk(tmp_path, **screening):
    s = settings()
    if screening:
        s = s.model_copy(update={"screening": {**s.screening, **screening}})
    d = fake_desk(s, FAKE_TODAY, now=Clock())
    fake_rendering(d, tmp_path)
    return d


def llm(desk):
    return desk.deps.llm(desk.deps.config())


class Fetch:
    def __init__(self):
        self.calls = 0

    def __call__(self, progress):
        self.calls += 1
        progress("Searching")
        return "Job search done: 3 new jobs.\n\n\U0001F449 Next: /screen checks the new jobs."


def statuses(desk):
    return {pid: v.get("Status") for pid, v in desk.repo.rows.items()}


def test_one_run_approves_builds_finds_contacts_and_drafts(tmp_path):
    desk = make_desk(tmp_path)
    fetch = Fetch()
    replies = desk.autopilot(fetch)
    assert fetch.calls == 1
    summary = replies[0].text
    assert summary.startswith("Job search done: 3 new jobs.")
    assert "Sent 10 jobs to the half-price batch" in summary
    assert batch.COLLECT_HINT not in summary  # autopilot collects by itself
    assert "Autopilot done: 6 jobs approved (4 Apply high, 2 Apply normal)." in summary
    assert ("2. Vistula Cloud, DevOps Engineer (Apply high): resume saved, 4 contacts, "
            "DRY RUN: 4 drafts not created") in summary
    assert "Drafts are in Gmail and never sent by themselves" in summary
    # A job without a description goes back to /pending and does not use an approval.
    assert "Vistula Cloud, Platform Engineer (Apply normal): resume not built" in summary
    assert desk.repo.rows["already-screened"]["Status"] == "Screened"
    assert "already-screened" in [r.page_id for r in desk.ranked()]
    status = statuses(desk)
    assert status["pl-clean"] in BUILDABLE_STATUSES
    assert status["nl-sponsor-yes"] in BUILDABLE_STATUSES
    assert desk.state.get(autopilot.DAY_KEY) == {"date": FAKE_TODAY.isoformat(), "count": 6}
    # Each saved resume comes as a PDF with an "I applied" button.
    documents = [r for r in replies if r.document]
    assert len(documents) == 6
    assert documents[1].document == ("Alex_Devops_VistulaCloud.pdf", b"%PDF-fake")
    assert documents[1].buttons == [("I applied", "ia:pl-clean")]
    assert documents[1].text == "Vistula Cloud, DevOps Engineer: resume saved"
    # Companies without a known email domain ask for it; a reply carries on as before.
    asks = [r.text for r in replies if r.text.startswith("What is the email domain")]
    assert "What is the email domain for Tulip Data B.V.?" in asks[0]
    # Apply low and Needs review are never approved by autopilot.
    for row in desk.ranked():
        assert row.screen_verdict not in ("Apply high", "Apply normal") or \
            row.page_id == "already-screened"


def test_daily_limit_and_second_run(tmp_path):
    desk = make_desk(tmp_path, autopilot_approvals=2)
    summary = desk.autopilot(None)[0].text
    assert "Autopilot done: 2 jobs approved (2 Apply high)." in summary
    assert "more wait for tomorrow (at most 2 a day)." in summary
    assert desk.state.get(autopilot.DAY_KEY)["count"] == 2
    again = desk.autopilot(None)[0].text
    assert "no new Apply high or Apply normal job approved" in again
    assert "Today's limit (2) is reached" in again


def test_waits_for_the_batch_then_carries_on(tmp_path):
    desk = make_desk(tmp_path)
    llm(desk).batch_ended = False
    fetch = Fetch()
    replies = desk.autopilot(fetch)
    text = replies[0].text
    assert len(replies) == 1
    assert "The batch is still working" in text
    assert "I check the batch every 5 minutes and carry on by myself" in text
    assert desk.state.get(autopilot.RUN_KEY) == {"since": datetime(2026, 10, 1, 9, 0).isoformat()}
    assert not any(v == "Approved" or v in BUILDABLE_STATUSES for v in statuses(desk).values())
    assert desk.autopilot_tick() is None  # still working: nothing to say
    # /autopilot again while it waits: no second fetch, no second batch.
    again = desk.autopilot(fetch)[0].text
    assert fetch.calls == 1
    assert again.startswith("Autopilot: carrying on with the batch that was sent before")
    llm(desk).batch_ended = True
    replies = desk.autopilot_tick()
    assert replies[0].text.startswith("Autopilot: the half-price batch has answered.")
    assert "Autopilot done: 6 jobs approved" in replies[0].text
    assert desk.state.get(autopilot.RUN_KEY) is None
    assert not batch.waiting_rows(desk.deps)
    assert desk.autopilot_tick() is None  # nothing waits any more


def test_webhook_mode_asks_to_send_it_again(tmp_path):
    desk = make_desk(tmp_path)
    desk.s = desk.s.model_copy(update={"bot_mode": "webhook"})
    llm(desk).batch_ended = False
    text = desk.autopilot(None)[0].text
    assert "Send /autopilot again in about an hour" in text


def test_nothing_without_a_target(tmp_path):
    desk = make_desk(tmp_path)
    desk.deps.repo = None
    assert desk.autopilot(None)[0].text.startswith("DRY RUN: no Job Opportunities target")
    assert desk.autopilot_tick() is None


def test_bot_command_progress_summary_and_pdfs(tmp_path):
    desk = make_desk(tmp_path)
    fake = ButtonTelegram([[update(1, "/autopilot")]])
    client = tb.TelegramClient(fake)
    fetch = Fetch()
    tb.poll_once(client, settings(), None, fetch=fetch, desk=desk)
    texts = [t for _, t in fake.sent]
    assert texts[0].startswith("[LOCAL] \U0001F50E Autopilot started")
    summary = next(t for t in texts if "Autopilot done" in t)
    assert "Job search done: 3 new jobs." in summary
    assert "Next: /screen" not in summary  # autopilot does that step itself
    assert ("Alex_Devops_VistulaCloud.pdf", b"%PDF-fake") in fake.documents
    assert ["ia:pl-clean"] in fake.buttons
    assert "autopilot" in tb.STALE_COMMANDS
    assert "autopilot" in [c["command"] for c in tb.bot_commands()]


def test_polling_loop_checks_a_waiting_batch(tmp_path):
    desk = make_desk(tmp_path)
    llm(desk).batch_ended = False
    desk.autopilot(None)
    llm(desk).batch_ended = True
    fake = ButtonTelegram([[], []])
    ticks = iter([0.0, 0.0, 10.0, 10.0])
    tb.run(settings(), tb.TelegramClient(fake), max_polls=2, desk=desk,
           clock=lambda: next(ticks, 1000.0))
    texts = [t for _, t in fake.sent]
    assert any("Autopilot: the half-price batch has answered." in t for t in texts)
    assert sum("Autopilot done" in t for t in texts) == 1


@pytest.mark.parametrize("minutes, seconds", [(5, 300), (0, 60)])
def test_check_interval(tmp_path, minutes, seconds):
    desk = make_desk(tmp_path, autopilot_check_minutes=minutes)
    assert autopilot.check_seconds(desk) == seconds
