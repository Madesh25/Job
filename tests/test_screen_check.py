"""Weekly screening check (S3, 23 Nov, your ask): 10 of the week's verdicts to tick in Notion,
last week's result and its wrong ones in the Error Log. Made-up jobs, no Notion."""

from datetime import datetime, timedelta, timezone

from test_telegram_bot import CHAT_ID, Clock, settings

from jobengine import telegram_bot as tb
from jobengine.bot_state import FakeBotState
from jobengine.notion_repo import FakeTable
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import screen_check as sc

IST = timezone(timedelta(hours=5, minutes=30))
SUNDAY = datetime(2026, 11, 29, 19, 45, tzinfo=IST)  # 2026-W48


def jobs(today=None):
    today = today or SUNDAY.date()
    out = []
    verdicts = ["Skip"] * 12 + ["Apply normal"] * 5 + ["Apply high", "Needs review", "Apply low"]
    for n, verdict in enumerate(verdicts):
        out.append((f"job-{n:02d}", {
            "Company": f"Co {n}", "Role": "DevOps Engineer", "Country": "Poland",
            "Screen verdict": verdict, "Swept date": today - timedelta(days=n % 6),
            "Skip reason": "Too senior" if verdict == "Skip" else None,
            "Gaps": "Kafka; Go" if verdict != "Skip" else None,
            "URL": f"https://jobs.example.com/{n} or https://other.example.com/{n}"}))
    out.append(("old-job", {"Company": "Old", "Role": "SRE", "Screen verdict": "Apply high",
                            "Swept date": today - timedelta(days=9)}))
    out.append(("no-verdict", {"Company": "New", "Role": "SRE", "Swept date": today}))
    return out


def test_ten_picks_spread_over_the_verdicts_and_the_same_each_time():
    picks = sc.pick(jobs(), SUNDAY.date())
    assert len(picks) == 10
    verdicts = [v["Screen verdict"] for _, v in picks]
    assert set(verdicts) == set(sc.VERDICTS)  # every verdict once before any twice
    assert verdicts[:5] == list(sc.VERDICTS)
    assert "old-job" not in {pid for pid, _ in picks}  # 9 days old
    assert sc.pick(jobs(), SUNDAY.date()) == picks  # the same week, the same picks
    assert sc.pick(jobs(), SUNDAY.date(), n=3) == picks[:3]


def test_the_row_says_why_with_both_links():
    pid, values = next(j for j in jobs() if j[1]["Screen verdict"] == "Skip")
    row = sc.row(pid, values, "2026-W48", SUNDAY.date())
    assert row["Job"] == "Co 0, DevOps Engineer" and row["Verdict"] == "Skip"
    assert row["Why"].startswith("Skip reason: Too senior")
    assert row["Job page"] == "https://www.notion.so/job00"
    assert row["Job link"] == "https://jobs.example.com/0"
    assert row["Key"] == "2026-W48|job-00"
    assert sc.why({}) == "No reason saved on the job (only the verdict)."


def test_sunday_evening_once_a_week():
    state = FakeBotState()
    assert sc.due(state, SUNDAY)
    assert not sc.due(state, SUNDAY.replace(hour=19, minute=0))
    assert not sc.due(state, SUNDAY - timedelta(days=1))
    sc.mark_sent(state, SUNDAY.date())
    assert not sc.due(state, SUNDAY + timedelta(minutes=30))
    assert sc.due(state, SUNDAY + timedelta(days=7))


def test_a_week_then_the_next_with_its_result_and_the_wrong_ones_reported():
    table, errors, state = FakeTable(), FakeTable(), FakeBotState()
    text = sc.run(table, errors, state, jobs(), SUNDAY)
    assert text.startswith("🧪 Weekly screening check (2026-W48): 10 of this week's verdicts")
    assert "Last week" not in text and len(table.created) == 10
    # you tick: 8 right, 1 wrong with Should be, 1 open
    for row in table.created[:8]:
        row["Your check"] = "Right"
    table.created[8].update({"Your check": "Wrong", "Should be": "Needs review",
                             "Note": "the job asks for Kafka in production"})
    assert sc.run(table, errors, state, jobs(), SUNDAY) and len(table.created) == 10  # again
    assert table.created[0]["Your check"] == "Right"  # your ticks are kept
    next_week = SUNDAY + timedelta(days=7)
    text = sc.run(table, errors, state, jobs(next_week.date()), next_week)
    assert "Last week (2026-W48): 8 right, 1 wrong, 1 not ticked." in text
    wrong = table.created[8]
    assert f"❌ {wrong['Job']}: {wrong['Verdict']}, should be Needs review" in text
    [error] = errors.created
    assert error["Level"] == "Reported by you" and error["Stage"] == "Screen"
    assert error["Problem"].startswith("Screening wrong: ")
    assert "Kafka in production" in error["Example"]
    sc.run(table, errors, state, jobs(next_week.date()), next_week)
    assert len(errors.created) == 1  # reported once


def test_no_screened_job_says_so():
    text = sc.run(FakeTable(), FakeTable(), FakeBotState(), [], SUNDAY)
    assert "no job was screened in the last 7 days" in text


def test_screencheck_command_on_the_desk():
    desk = fake_desk(settings(), FAKE_TODAY, now=Clock())
    desk.screen()
    update = {"update_id": 1, "message": {"chat": {"id": int(CHAT_ID)}, "text": "/screencheck"}}
    [reply] = tb.desk_replies(update, settings(), desk)
    assert reply.text.startswith("🧪 Weekly screening check (")
    desk.screen_check_table = None
    [reply] = tb.desk_replies(update, settings(), desk)
    assert reply.text.startswith("The screening check needs Notion")
    assert "/screencheck - 10 of the week's verdicts" in tb.HELP_TEXT
