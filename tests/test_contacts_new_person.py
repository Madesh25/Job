"""LOG-18 (25 Nov, your rules): a person who did not answer 14 days after the follow-up
(Ghosted) or who says they cannot help is never mailed again for that company; its next job
gets a new person."""

from datetime import date, datetime

from test_contact_replies import row

from jobengine.contacts import cache
from jobengine.contacts.models import Mix
from jobengine.mail import drafter
from jobengine.settings import load_settings
from jobengine.track import reply_analysis, runner
from jobengine.track.models import CANT_HELP, CLASSES, Classification
from jobengine.track.status import EFFECTS, effect

NOW = datetime.fromisoformat("2026-10-15T08:00:00+05:30")
TODAY = NOW.date()


def test_a_ghosted_person_is_not_picked_again_for_the_next_job():
    rows = [row("ghost", Status="Ghosted", Replied=False), row("new")]
    picked = cache.pick(rows, Mix(peer=1, hiring=0, recruiter=0), "Poland", TODAY, 6)
    assert [c.email for c in picked] == ["new@acme.example.com"]
    assert not cache.usable(rows[0], TODAY, 6)


def test_ghosted_and_dead_end_are_never_drafted():
    line = drafter.DraftLine(page_id="p", name="Gil", email="gil@acme.example.com",
                             type="Peer engineer")
    for status in ("Ghosted", "Dead end"):
        assert drafter._skip_reason({"Status": status}, line, None, TODAY, 30, [], None,
                                    set()) == status


def test_cant_help_is_a_reply_class_that_ends_the_person_not_the_job():
    assert CANT_HELP in CLASSES
    assert EFFECTS[CANT_HELP] == (None, "Dead end")
    assert effect(CANT_HELP, "Applied", "Contacted").job_status is None
    assert reply_analysis.group_of(CANT_HELP) == "rejection"
    assert ("Can't help", "cant_help") in runner.CHOICES


def test_a_cant_help_reply_makes_a_dead_end_and_a_note(monkeypatch):
    d = runner.fake_deps(load_settings("local", {"DRY_RUN": "false"}))
    piotr = d.contacts.rows["c-piotr"]
    piotr["Replied"] = True  # a friendly answer before does not keep them
    monkeypatch.setattr(runner, "classify",
                        lambda *a, **k: Classification(CANT_HELP, 0.95, quote="cannot help"))
    runner.run_daily(d, NOW)
    assert piotr["Status"] == "Dead end"
    assert "said they cannot help 2026-10-15" in piotr["Notes"]
    for pid in piotr.get("Related jobs") or []:  # the job goes on: never Rejected by this
        assert d.jobs.rows[pid].get("Status") not in ("Rejected", "Replied")
    assert not cache.usable(cache.CachedRow("c-piotr", piotr), date(2026, 10, 20), 6)
