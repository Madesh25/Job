import base64
from datetime import date, datetime
from email import message_from_bytes, policy

import pytest

from jobengine.bot_state import FakeBotState
from jobengine.gmail_client import FakeGmail
from jobengine.llm import FakeLLM
from jobengine.settings import load_settings
from jobengine.track import runner
from jobengine.track.fakes import load_threads

NOW = datetime.fromisoformat("2026-10-15T08:00:00+05:30")


def settings(env="local", dry="false"):
    return load_settings(env, {"DRY_RUN": dry})


def deps(env="local", dry="false", **kw):
    return runner.fake_deps(settings(env, dry), **kw)


def run(d, now=NOW):
    return runner.run_daily(d, now)


def contact(d, pid):
    return d.contacts.rows[pid]


def job(d, pid):
    return d.jobs.get_values(pid)


def test_sent_draft_becomes_contacted_with_the_send_date():
    d = deps()
    report = run(d)
    anna = contact(d, "c-anna")
    assert (anna["Status"], anna["Last contacted"], anna["Gmail draft ID"]) == \
        ("Contacted", date(2026, 10, 6), "")
    assert job(d, "job-vistula")["Last activity date"] is not None
    assert "Anna Example" in report.sent
    assert contact(d, "c-jan")["Status"] == "Drafted"  # its draft still exists


def test_deleted_draft_goes_back_with_ids_cleared():
    d = deps()
    run(d)
    kasia = contact(d, "c-kasia")
    assert (kasia["Status"], kasia["Gmail draft ID"], kasia["Gmail thread ID"]) == \
        ("Unverified", "", "")
    assert "draft deleted 2026-10-15" in kasia["Notes"]


def test_deleted_draft_of_verified_contact_goes_back_to_verified():
    d = deps()
    d.contacts.rows["c-kasia"]["Notes"] = "drafted for DevOps Engineer 2026-10-05"
    run(d)
    assert contact(d, "c-kasia")["Status"] == "Verified"


def test_followup_sent_is_detected():
    d = deps()
    run(d)
    zofia = contact(d, "c-zofia")
    assert (zofia["Status"], zofia["Last contacted"], zofia["Follow-up draft ID"]) == \
        ("Followed up", date(2026, 10, 12), "")


def test_reply_sets_replied_and_moves_the_job():
    d = deps()
    report = run(d)
    assert contact(d, "c-piotr")["Status"] == "Replied"
    assert job(d, "job-vistula")["Status"] == "Interview"
    assert "Piotr Example (Vistula Cloud) -> Interview" in report.replies


def test_auto_reply_and_own_messages_are_not_replies():
    d = deps()
    run(d)
    assert contact(d, "c-ola")["Status"] == "Contacted"  # only an auto-reply came back
    assert contact(d, "c-tomek")["Status"] == "Contacted"  # only your own message


def test_opt_out_phrase_needs_no_llm_and_blocks_followups():
    llm = FakeLLM()
    d = deps()
    d.llm = lambda config: llm
    report = run(d)
    rita = contact(d, "c-rita")
    assert rita["Status"] == "Do not contact"
    assert rita["Follow-up draft ID"] == ""
    assert all("reply_m-rita" not in (c["key"] or "") for c in llm.calls)
    assert any("follow-up draft for Rita Example is no longer needed" in n
               for n in report.notes)
    assert "Rita Example" not in report.followups


def test_low_confidence_goes_to_a_card_and_changes_nothing_until_tapped():
    d = deps()
    report = run(d)
    assert contact(d, "c-marek")["Status"] == "Contacted"
    card = next(c for c in report.cards if "Marek Example" in c.text)
    assert ("Interview", "rc:c:cmarek:interview") in card.buttons
    assert "Marek Example" not in report.followups  # a reply waits for your call
    again = run(d, NOW.replace(day=16))
    assert not any("Marek Example" in c.text for c in again.cards)  # asked once
    text = runner.apply_choice(d, NOW, "rc:c:cmarek:screening")
    assert text == "Applied: Marek Example (Vistula Cloud) -> Replied"
    assert contact(d, "c-marek")["Status"] == "Replied"
    assert job(d, "job-vistula")["Status"] == "Interview"  # Screening is not forward of it
    assert runner.apply_choice(d, NOW, "rc:c:cmarek:ignore") == "Ignored."


def test_job_thread_rejection_and_interview():
    d = deps()
    report = run(d)
    assert job(d, "job-canal")["Status"] == "Rejected"
    assert job(d, "job-harbor")["Status"] == "Interview"
    assert "Canal Payments -> Rejected" in report.applications


def test_bounces_from_header_and_dsn():
    d = deps()
    report = run(d)
    assert contact(d, "c-bob")["Status"] == "Bounced"
    assert contact(d, "c-olga")["Status"] == "Bounced"
    assert "bounced 2026-10-04" in contact(d, "c-bob")["Notes"]
    assert report.bounced == ["bob.example@vistula.example.com",
                              "olga.example@northwind.example.org"]


def test_followups_on_day_seven_not_six_and_never_twice():
    d = deps()
    report = run(d)
    assert sorted(report.followups) == ["Anna Example", "Tomek Example"]  # 9 and 10 days
    assert contact(d, "c-tomek")["Follow-up draft ID"].startswith("r-fake-draft-")
    assert contact(d, "c-ola").get("Follow-up draft ID") in (None, "")  # 5 days: not yet
    # Day 7 exactly for a fresh contact, not day 6.
    d2 = deps()
    d2.contacts.rows["c-tomek"]["Last contacted"] = date(2026, 10, 9)
    assert "Tomek Example" not in run(d2).followups
    d3 = deps()
    d3.contacts.rows["c-tomek"]["Last contacted"] = date(2026, 10, 8)
    assert "Tomek Example" in run(d3).followups
    # Never twice, even after the follow-up draft was deleted in Gmail.
    gmail = d.gmail()
    gmail.open_drafts.clear()
    again = run(d, NOW.replace(day=16))
    assert again.followups == []


def test_followup_is_a_reply_in_the_same_thread():
    gmail = FakeGmail(threads=load_threads(), open_drafts={"d-jan", "f-rita"})
    d = deps(gmail=gmail)
    run(d)
    tomek = next(x for x in gmail.drafts if x["thread"] == "t-tomek")
    msg = message_from_bytes(base64.urlsafe_b64decode(tomek["raw"]), policy=policy.default)
    assert msg["In-Reply-To"] == "<m-tomek-1@mail.example.com>"
    assert msg["References"] == "<m-tomek-1@mail.example.com>"
    assert msg["Subject"] == ("[LOCAL] to tomek.example@vistula.example.com | Re: DevOps "
                              "Engineer role at Vistula Cloud Poland")
    assert msg["To"] == "madeshwaranm02@gmail.com"
    assert not any(p.get_content_type() == "application/pdf" for p in msg.walk())
    assert "Hi Tomek," in msg.get_body(("plain",)).get_content()


def test_no_followup_for_replied_bounced_or_do_not_contact():
    d = deps()
    for pid, status in (("c-tomek", "Replied"), ("c-anna", "Bounced"),
                        ("c-marek", "Do not contact")):
        d.contacts.rows[pid]["Status"] = status
    d.contacts.rows["c-anna"]["Gmail draft ID"] = ""
    report = run(d)
    assert report.followups == []


def test_dry_run_drafts_no_followup_and_applies_no_label():
    gmail = FakeGmail(threads=load_threads(), open_drafts={"d-jan", "f-rita"},
                      label_ids={"JobSearch/Replies": "L1", "JobSearch/Applications": "L2"})
    d = deps(dry="true", gmail=gmail)
    report = run(d)
    assert gmail.drafts == [] and gmail.label_calls == []
    assert sorted(report.followups_dry) == ["Anna Example", "Tomek Example"]
    assert contact(d, "c-piotr")["Status"] == "Replied"  # sandbox Notion writes still happen


def test_labels_applied_and_missing_one_reported_not_created():
    gmail = FakeGmail(threads=load_threads(), open_drafts={"d-jan", "f-rita"},
                      label_ids={"JobSearch/Replies": "L1", "JobSearch/Applications": "L2"})
    d = deps(gmail=gmail)
    report = run(d)
    assert ("t-piotr", "L1") in gmail.label_calls
    assert ("t-harbor-job", "L2") in gmail.label_calls
    missing = [n for n in report.notes if "JobSearch/Interviews not found" in n]
    assert len(missing) == 1


def test_ghosted_contact_and_job():
    d = deps()
    report = run(d)
    assert contact(d, "c-olek")["Status"] == "Ghosted"
    assert job(d, "job-baltic")["Status"] == "Ghosted"
    assert report.ghosted_jobs == ["Baltic Example Bank"]
    assert job(d, "job-vistula")["Status"] != "Ghosted"  # contacts still open


def test_ghosted_job_waits_for_open_contacts():
    d = deps()
    d.contacts.rows["c-bartek"]["Status"] = "Contacted"
    d.contacts.rows["c-bartek"]["Last contacted"] = date(2026, 10, 14)
    run(d)
    assert job(d, "job-baltic")["Status"] == "Followed up"


def test_linking_one_candidate_links_two_ask():
    d = deps()
    report = run(d)
    assert job(d, "job-northwind")["Gmail thread ID"] == "t-northwind-ats"
    assert "Northwind Cloud linked" in report.applications
    assert job(d, "job-fjord").get("Gmail thread ID") in (None, "")
    links = [c for c in report.cards if "Fjord Data" in c.text]
    assert len(links) == 2 and links[0].buttons[0] == ("Link", "lk:jobfjord:t-fjord-a")
    assert runner.link_choice(d, NOW, "lk:jobfjord:-") == "OK, not linked."
    assert runner.link_choice(d, NOW, "lk:jobfjord:t-fjord-a").startswith("Linked Fjord Data")
    assert job(d, "job-fjord")["Gmail thread ID"] == "t-fjord-a"


def test_window_and_marker():
    state = FakeBotState()
    d = deps(state=state)
    first = run(d)
    assert first.since == datetime.fromisoformat("2026-10-01T08:00:00+05:30")
    assert state.get(runner.MARKER) == {"at": NOW.isoformat()}
    later = NOW.replace(day=17)
    second = run(d, later)
    assert second.since == datetime.fromisoformat("2026-10-14T08:00:00+05:30")


def test_failed_step_does_not_move_the_marker():
    state = FakeBotState()
    state.set(runner.MARKER, {"at": "2026-10-10T08:00:00+05:30"})
    gmail = FakeGmail(threads=load_threads())

    def broken(query, limit=100):
        raise RuntimeError("Gmail is down")

    gmail.search = broken
    d = deps(state=state, gmail=gmail)
    report = run(d)
    assert not report.ok and any("Gmail is down" in e for e in report.errors)
    assert state.get(runner.MARKER) == {"at": "2026-10-10T08:00:00+05:30"}
    assert "Error: bounces failed: Gmail is down" in report.text()


def test_token_failure_is_reported_and_does_not_crash():
    d = deps()
    d.token_check = lambda name: "invalid_grant" if name == "sender" else None
    report = run(d)
    text = report.text()
    assert text.startswith("Gmail token for sender failed: invalid_grant. Regenerate it "
                           "(docs/gmail.md).")
    assert "Tokens: alerts OK, sender FAILED" in text
    assert report.sent == [] and contact(d, "c-anna")["Status"] == "Drafted"  # skipped
    assert report.retention is not None  # Notion-only step still runs
    assert d.state.get(runner.MARKER) is None


def test_retention_only_after_tap_and_only_in_prod():
    d = deps()
    report = run(d)
    assert report.retention.buttons == [("Delete 1 old contacts", "rd:20261015")]
    assert "c-old" in d.contacts.rows  # listed, not deleted
    assert runner.retention_choice(d, NOW, "rd:20261015").startswith("DRY RUN: would delete 1")
    assert "c-old" in d.contacts.rows
    prod = deps("prod")
    assert runner.retention_choice(prod, NOW, "rd:20261015") == \
        "Deleted 1 old contacts (moved to the Notion trash)."
    assert "c-old" not in prod.contacts.rows and "c-old-dnc" in prod.contacts.rows


def test_no_write_changes_nothing():
    d = deps(write=False)
    run(d)
    assert d.contacts.writes == [] and d.jobs.writes == []
    assert d.state.get(runner.MARKER) is None


@pytest.mark.parametrize("status", ["Rejected", "Offer", "Withdrawn", "Declined"])
def test_closed_jobs_are_never_changed_by_the_run(status):
    d = deps()
    d.jobs.rows["job-canal"]["Status"] = status
    run(d)
    assert job(d, "job-canal")["Status"] == status


def test_weekly_digest_compares_weeks():
    from jobengine.track.digest import run_weekly_digest

    d = deps()
    text = run_weekly_digest(d, NOW.replace(day=11))
    assert text.startswith("Weekly digest 2026-10-11")
    # Week 5 to 11 Oct: Northwind, Fjord and Harbor applied; the week before: Vistula, Canal.
    assert "This week: applied 3 (+1)" in text
    assert "Unsent drafts: 3 cold mails, 2 follow-ups" in text
    assert "Strategy gate:" in text
