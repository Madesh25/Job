"""Reply analysis (S9, 23 Nov, your ask): every reply counted by group, and the weekly "what
got replies" by resume headline and by skill. Made-up jobs and replies."""

from datetime import date, timedelta

from test_telegram_bot import Clock, settings

from jobengine.bot_state import FakeBotState
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import day_ledger
from jobengine.track import reply_analysis as ra

TODAY = date(2026, 11, 29)
D = date(2026, 11, 10)


def job(role, status, stack=(), applied=D):
    return {"Role": role, "Status": status, "Applied date": applied, "Tech stack": list(stack)}


JOBS = [
    job("Senior DevOps Engineer", "Interview", ["Kubernetes", "Terraform"]),
    job("DevOps Engineer", "Replied", ["Kubernetes", "AWS"]),
    job("DevOps Engineer", "Applied", ["Kubernetes"]),
    job("Cloud Engineer", "Rejected", ["Azure", "Terraform"]),
    job("Cloud Engineer", "Applied", ["Azure"]),
    job("Site Reliability Engineer", "Ghosted", ["Go"]),
    {"Role": "DevOps Engineer", "Status": "Screened", "Tech stack": ["Kubernetes"]},  # not applied
]


def headline(role):
    for title in ("DevOps Engineer", "Cloud Engineer"):
        if role and title in role:
            return title
    return None


def test_each_class_goes_to_one_of_four_groups():
    assert [ra.group_of(k) for k in ("interview", "screening", "offer")] == ["interview"] * 3
    assert ra.group_of("rejection") == ra.group_of("opt_out") == "rejection"
    assert ra.group_of("ack") == ra.group_of("auto_reply") == "automatic"
    assert ra.group_of("positive") == ra.group_of("other") == "question"


def test_replies_are_counted_per_day_and_summed_over_the_week():
    state = FakeBotState()
    clock = {"day": TODAY}
    day_ledger.bind(state, lambda: clock["day"])
    try:
        ra.record("interview")
        ra.record("ack")
        clock["day"] = TODAY - timedelta(days=3)
        ra.record("rejection")
        clock["day"] = TODAY - timedelta(days=9)  # older than the week
        ra.record("interview")
    finally:
        day_ledger.bind(None, date.today)
    counts = ra.week_counts(lambda d: day_ledger.day(state, d), TODAY)
    assert counts == {"interview": 1, "rejection": 1, "automatic": 1, "question": 0}


def test_what_got_replies_by_headline_and_skill():
    heads, skills = ra.tallies(JOBS, headline)
    assert (heads["DevOps Engineer"].applied, heads["DevOps Engineer"].answered) == (3, 2)
    assert heads["Cloud Engineer"].rejected == 1
    assert heads[ra.DEFAULT_HEADLINE].applied == 1  # the SRE role names no approved title
    assert skills["Kubernetes"].applied == 3 and skills["Kubernetes"].answered == 2
    text = ra.text({"interview": 1, "rejection": 0, "automatic": 2, "question": 1}, JOBS,
                   headline)
    lines = text.splitlines()
    assert lines[0] == "📬 Replies this week: 1 interview, 0 rejection, 2 automatic, 1 question."
    assert "- DevOps Engineer: 2 of 3 answered (67%), 0 rejected" in lines
    assert "- Cloud Engineer: 0 of 2 answered (0%), 1 rejected" in lines
    assert "- Kubernetes: 2 of 3 answered (67%), 0 rejected" in lines
    assert "- Go: " not in text  # only one application
    assert lines[-1] == "Most answers: the headline \"DevOps Engineer\" (2 of 3)."


def test_nothing_applied_and_no_answers_yet():
    assert ra.text({}, [], headline).endswith("What got replies: nothing applied yet.")
    silent = [job("DevOps Engineer", "Applied")] * 10
    assert ra.text({}, silent, headline).endswith("(Phase 23 S8 ATS check).")


def test_the_weekly_digest_has_the_block():
    desk = fake_desk(settings(), FAKE_TODAY, now=Clock())
    [digest] = desk.digest_command()
    assert "📬 Replies this week: " in digest.text
