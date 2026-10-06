"""Contacts memory (6 Oct): each mail sent is counted ("Mails sent"), a contact who answers
is ticked "Replied" and reused for the company's next jobs, and one who never answers
becomes "Dead end" after 5 mails and is never mailed again."""

from datetime import date, datetime

from jobengine.contacts import cache
from jobengine.contacts.models import Mix
from jobengine.mail import drafter
from jobengine.settings import load_settings
from jobengine.track import runner

NOW = datetime.fromisoformat("2026-10-15T08:00:00+05:30")
TODAY = NOW.date()


def deps():
    return runner.fake_deps(load_settings("local", {"DRY_RUN": "false"}))


def anna(d):
    return d.contacts.rows["c-anna"]


def test_a_sent_first_mail_is_counted():
    d = deps()
    runner.run_daily(d, NOW)
    assert (anna(d)["Status"], anna(d)["Mails sent"]) == ("Contacted", 1)


def test_the_fifth_mail_without_an_answer_is_a_dead_end():
    d = deps()
    anna(d)["Mails sent"] = 4
    runner.run_daily(d, NOW)
    assert (anna(d)["Status"], anna(d)["Mails sent"]) == ("Dead end", 5)


def test_someone_who_answered_is_never_a_dead_end():
    d = deps()
    anna(d).update({"Mails sent": 7, "Replied": True})
    runner.run_daily(d, NOW)
    assert (anna(d)["Status"], anna(d)["Mails sent"]) == ("Contacted", 8)


def test_a_mail_for_another_job_to_a_contacted_person_is_counted():
    d = deps()
    anna(d).update({"Status": "Contacted", "Last contacted": date(2026, 9, 20),
                    "Mails sent": 2})
    report = runner.run_daily(d, NOW)
    assert (anna(d)["Status"], anna(d)["Mails sent"], anna(d)["Gmail draft ID"]) == \
        ("Contacted", 3, "")
    assert anna(d)["Last contacted"] == date(2026, 10, 6)
    assert "Anna Example (mail 3)" in report.sent


def test_a_deleted_draft_for_a_contacted_person_only_clears_the_draft():
    d = deps()
    anna(d).update({"Status": "Contacted", "Last contacted": date(2026, 10, 6),
                    "Mails sent": 2})
    runner.run_daily(d, NOW)
    assert (anna(d)["Status"], anna(d)["Mails sent"], anna(d)["Gmail draft ID"]) == \
        ("Contacted", 2, "")


def test_a_reply_ticks_replied():
    d = deps()
    runner.run_daily(d, NOW)
    assert d.contacts.rows["c-piotr"]["Replied"] is True


def row(pid, **values):
    base = {"Company": "Acme", "Type": "Peer engineer", "Country": "Poland",
            "Email": f"{pid}@acme.example.com", "Date found": date(2026, 9, 1)}
    return cache.CachedRow(pid, {**base, **values})


def test_people_who_answered_are_reused_first_even_when_old():
    rows = [row("new"), row("old", **{"Date found": date(2024, 1, 1), "Replied": True}),
            row("dead", Status="Dead end")]
    picked = cache.pick(rows, Mix(peer=1, hiring=0, recruiter=0), "Poland", TODAY, 6)
    assert [c.email for c in picked] == ["old@acme.example.com"]
    assert not cache.usable(rows[2], TODAY, 6)


def test_dead_end_is_never_drafted_and_kept_by_retention():
    line = drafter.DraftLine(page_id="p", name="Dan", email="dan@acme.example.com",
                             type="Peer engineer")
    reason = drafter._skip_reason({"Status": "Dead end"}, line, None, TODAY, 30, [], None,
                                  set())
    assert reason == "Dead end"
    contacts = {"a": {"Status": "Dead end", "Date found": date(2024, 1, 1)},
                "b": {"Status": "Contacted", "Replied": True, "Date found": date(2024, 1, 1)},
                "c": {"Status": "Contacted", "Date found": date(2024, 1, 1)}}
    assert runner.retention_candidates(contacts, TODAY, 12) == ["c"]
