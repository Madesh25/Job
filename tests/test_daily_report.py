"""Daily report and failure alerts (Autopilot H1 and H3, 25 Oct): the day ledger, the report
text, its 21:45 India-time schedule, the email copy and the failure mail."""

from datetime import UTC, date, datetime, timedelta, timezone

from test_telegram_bot import Clock, settings

from jobengine import alerts
from jobengine import telegram_bot as tb
from jobengine.bot_state import FakeBotState
from jobengine.screen.desk import Reply, fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import costs, daily_report, day_ledger

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 10, 1)


class Config(dict):
    def get(self, key, default=None):  # ConfigStore.get answers None for a missing key
        return super().get(key, default)


def test_the_ledger_adds_up_today_and_keeps_8_days():
    state = FakeBotState()
    clock = {"day": DAY}
    day_ledger.bind(state, lambda: clock["day"], lambda: datetime(2026, 10, 1, 7, 2))
    try:
        day_ledger.add_sweep({"gmail": 30, "adzuna": 9}, new=12, updated=3)
        day_ledger.count("screened.Apply high", 2)
        day_ledger.count("screened.Skip")
        day_ledger.count("resumes")
        day_ledger.add_match(9, 12)
        costs.record(state, DAY, "score", 0.05)
        entry = day_ledger.day(state, DAY)
        assert entry["sweeps"] == [{"at": "07:02", "sources": {"gmail": 30, "adzuna": 9},
                                    "new": 12, "updated": 3}]
        assert entry["screened.Apply high"] == 2 and entry["screened.Skip"] == 1
        assert entry["matches"] == [[9, 12]] and entry["cost"] == 0.05
        clock["day"] = DAY + timedelta(days=9)
        day_ledger.count("resumes")
        assert list(state.get(day_ledger.KEY)["days"]) == [(DAY + timedelta(days=9)).isoformat()]
    finally:
        day_ledger.bind(None, date.today)
    day_ledger.count("resumes")  # not bound: nothing happens, nothing breaks


def test_the_report_text_and_an_empty_day():
    entry = {"sweeps": [{"at": "07:02", "sources": {"gmail": 30, "adzuna": 9}, "new": 12},
                        {"at": "19:31", "sources": {"gmail": 8, "ats": 2}, "new": 4}],
             "screened.Apply high": 2, "screened.Apply normal": 5, "screened.Skip": 9,
             "resumes": 3, "matches": [[9, 12], [6, 12], [10, 12]], "contacts": 4,
             "drafts": 3, "mails_sent": 1, "replies": 1, "cost": 0.123}
    text = daily_report.text(entry, DAY, month_cost="AI in October: about $2.10.",
                             credits="Credits: Hunter 3/50",
                             tokens=["Gmail token m02: 5 day(s) left"],
                             waiting=["4 job(s) in /pending", "2 need a description (/jd)"])
    assert text.splitlines() == [
        "📊 Daily report, Thu 01 Oct",
        "🔎 Fetch: 2 run(s) (07:02, 19:31), 16 new job(s) saved. Found: Email alerts 38, "
        "Adzuna 9, Company sites 2.",
        "🧪 Screening: 16 screened (2 apply high, 5 apply normal, 9 skip).",
        "📄 Resumes: 3 built, keyword match on average 8 of 12 (lowest 6 of 12).",
        "👥 Contacts found: 4. ✉️ Gmail drafts: 3, mails sent: 1. 📩 Replies: 1.",
        "💰 AI today: about $0.12. AI in October: about $2.10.",
        "💳 Credits: Hunter 3/50",
        "🔑 Gmail token m02: 5 day(s) left",
        "📌 Waiting for you: 4 job(s) in /pending, 2 need a description (/jd)."]
    empty = daily_report.text({}, DAY, month_cost="", credits="Credits: -")
    assert "Nothing ran today: 0 new jobs, 0 screened." in empty
    assert "🔎 Fetch: no /fetch ran today." in empty


def test_the_report_comes_once_at_21_45_india_time():
    state, config = FakeBotState(), Config()
    assert not daily_report.due(config, state, datetime(2026, 10, 1, 21, 40, tzinfo=IST))
    assert daily_report.due(config, state, datetime(2026, 10, 1, 21, 45, tzinfo=IST))
    assert daily_report.due(config, state, datetime(2026, 10, 1, 16, 30, tzinfo=UTC))
    daily_report.mark_sent(state, DAY)
    assert not daily_report.due(config, state, datetime(2026, 10, 1, 22, 0, tzinfo=IST))
    assert not daily_report.due(config, state, datetime(2026, 10, 2, 1, 0, tzinfo=IST))  # late
    assert daily_report.due(config, state, datetime(2026, 10, 2, 21, 50, tzinfo=IST))
    off = Config({"schedule.report": "false"})
    assert not daily_report.due(off, FakeBotState(), datetime(2026, 10, 2, 21, 50, tzinfo=IST))
    later = Config({"schedule.report_time": "23:00"})
    assert not daily_report.due(later, FakeBotState(), datetime(2026, 10, 2, 21, 50, tzinfo=IST))


def test_the_desk_sends_the_report_by_telegram_and_email():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: datetime(2026, 10, 1, 21, 50)  # naive: tracking adds +05:30
    [reply] = d.daily_report_tick()
    assert reply.text.startswith("📊 Daily report, Thu 01 Oct")
    assert d.mails == [("Job Engine daily report 01 Oct", reply.text)]
    assert d.daily_report_tick() == []  # once a day
    assert d.report_command()[0].text.startswith("📊 Daily report")  # /report any time
    assert "report" in {c["command"] for c in tb.bot_commands()}


def test_a_failed_scheduled_step_is_mailed_once_an_hour():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: datetime(2026, 10, 1, 7, 5)

    def broken():
        raise RuntimeError("invalid_grant: Token has been expired")

    replies = tb._scheduled("Autopilot", broken, d)
    assert replies == [Reply("Autopilot failed: invalid_grant: Token has been expired")]
    [(subject, text)] = d.mails
    assert subject == "Job Engine: Autopilot failed"
    assert "Error: invalid_grant: Token has been expired" in text and "Never the .env" in text
    tb._scheduled("Autopilot", broken, d)
    assert len(d.mails) == 1  # the same failure within the hour: no second mail
    d._track_now = lambda: datetime(2026, 10, 1, 8, 10)
    tb._scheduled("Autopilot", broken, d)
    assert len(d.mails) == 2


def test_the_owner_mail_goes_to_you_only(monkeypatch):
    sent = []

    class Gmail:
        def create_draft(self, raw, thread_id=None):
            sent.append(raw)
            return type("D", (), {"draft_id": "d1"})()

        def send_draft(self, draft_id):
            sent.append(draft_id)

    from jobengine import gmail_client

    monkeypatch.setattr(gmail_client.GmailClient, "from_settings", classmethod(
        lambda cls, s: Gmail()))
    s = settings()
    assert alerts.owner_mail(s, "Job Engine daily report", "hello") is None
    import base64
    import email

    message = email.message_from_bytes(base64.urlsafe_b64decode(sent[0]))
    assert message["To"] == s.redirect_to and sent[1] == "d1"
    assert message["Subject"].endswith("Job Engine daily report")

    def refuse(cls, s):
        raise RuntimeError("no sender token")

    monkeypatch.setattr(gmail_client.GmailClient, "from_settings", classmethod(refuse))
    assert alerts.owner_mail(s, "x", "y") == "no sender token"


def test_the_report_and_a_failure_ping_the_heartbeat():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: datetime(2026, 10, 1, 21, 50)
    d.daily_report_tick()
    assert d.pings == [True]

    def broken():
        raise RuntimeError("Notion is down")

    tb._scheduled("Autopilot", broken, d)
    tb._scheduled("Autopilot", broken, d)  # mailed once an hour: pinged once too
    assert d.pings == [True, False]


def test_heartbeat_ping_goes_to_hc_ping_only(monkeypatch):
    from jobengine import heartbeat, http

    got = []
    monkeypatch.setattr(http, "get_text", lambda url, s=None: got.append(url) or "OK")
    assert not heartbeat.ping(settings())  # no HEALTHCHECK_URL: nothing sent
    s = settings(HEALTHCHECK_URL="https://hc-ping.com/abc-123")
    assert heartbeat.ping(s) and heartbeat.ping(s, ok=False)
    assert got == ["https://hc-ping.com/abc-123", "https://hc-ping.com/abc-123/fail"]
    assert not heartbeat.ping(settings(HEALTHCHECK_URL="https://evil.example.com/x"))

    def down(url, s=None):
        raise http.HttpError("GET hc-ping.com failed")

    monkeypatch.setattr(http, "get_text", down)
    assert not heartbeat.ping(s)  # logged, never raised


def _week(**entry):
    return [dict(entry) for _ in range(7)]


def test_warnings_compare_today_with_the_week():
    usual = {"sweeps": [{"at": "07:00", "sources": {"gmail": 20, "adzuna": 6}, "new": 9}],
             "screened.Skip": 4, "screened.Apply normal": 6, "matches": [[9, 12], [10, 12]]}
    today = {"sweeps": [{"at": "07:00", "sources": {"gmail": 0, "adzuna": 7}, "new": 2}],
             "screened.Skip": 9, "screened.Apply normal": 1, "matches": [[5, 12]]}
    lines = daily_report.warnings(today, _week(**usual), tokens=[("m02", 1), ("m01", 20)],
                                  used_up=["Hunter"], missed=["19:30"])
    assert lines == [
        "⚠️ The 19:30 autopilot did not run today: check /health and the bot log.",
        "⚠️ Email alerts found 0 jobs today (usually about 20 a day): check /sources.",
        "⚠️ Skip rate 90% today (usually 40%): check the skipped jobs in Notion (a rule or a "
        "source may be off).",
        "⚠️ Resume keyword match 42% today (usually 79%): look at one of today's resumes.",
        "⚠️ Gmail token m02 stops in 1 day(s): send /gmailtoken m02.",
        "⚠️ No contact credits left this month: Hunter."]
    assert daily_report.warnings(usual, _week(**usual)) == []  # a normal day
    assert daily_report.warnings(today, []) == []  # no week to compare with yet
    text = daily_report.text(today, DAY, month_cost="", credits="Credits: -", warned=lines[:1])
    assert text.splitlines()[1].startswith("⚠️ The 19:30 autopilot")


def test_used_up_credits_count_the_second_account():
    from jobengine.config_store import ConfigStore
    from jobengine.contacts.credits import CreditBook

    config = ConfigStore.from_values({"credits.hunter": "50 / 50 per month",
                                      "credits.hunter_2": "3 / 50 per month",
                                      "credits.snov": "50 / 50 per month"})
    assert CreditBook(config, DAY).used_up() == ["Snov"]


def test_a_missed_scheduled_run_is_named(tmp_path):
    from test_autopilot_schedule import Clock as IstClock
    from test_autopilot_schedule import make_desk

    from jobengine.screen import schedule

    d = make_desk(tmp_path, clock=IstClock(hour=21, minute=45))
    schedule.mark_started(d, schedule.plan(d).start)
    assert schedule.missed(d) == []  # 19:30 is still inside its 3 hours
    d = make_desk(tmp_path, clock=IstClock(hour=23, minute=0))
    assert schedule.missed(d) == ["07:00", "19:30"]
    assert schedule.missed(make_desk(tmp_path, on=False, clock=IstClock(hour=23))) == []


def test_a_short_outage_is_tried_again_before_it_is_told():
    """26 Oct: one "HTTP 500 Unexpected error occurred" from Notion became an alert."""
    tb._passing.clear()
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: datetime(2026, 10, 1, 7, 5)

    def broken():
        raise RuntimeError("POST https://api.notion.com/v1/data_sources/x/query failed: "
                           "HTTP 500 Unexpected error occurred")

    assert tb._scheduled("Auto-apply", broken, d) == []
    assert tb._scheduled("Auto-apply", broken, d) == []
    assert d.mails == []
    [told] = tb._scheduled("Auto-apply", broken, d)
    assert told.text.startswith("Auto-apply failed: POST") and len(d.mails) == 1
    assert tb._scheduled("Auto-apply", broken, d) == []  # counted again from zero
    assert tb._scheduled("Auto-apply", lambda: [], d) == [] and "Auto-apply" not in tb._passing
