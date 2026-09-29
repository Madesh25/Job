"""PR 7: referral ask for engineers, cold mail for HR, and the Apply high alert."""

import pytest
from test_telegram_bot import ButtonTelegram, Clock, settings, update

from jobengine import telegram_bot as tb
from jobengine.contacts.classify import classify_title
from jobengine.mail.fill import choose_template
from jobengine.mail.templates import KIND, fake_templates
from jobengine.screen import desk as desk_module
from jobengine.screen.desk import HIGH_ALERT, fake_desk
from jobengine.sweep.fakes import FAKE_TODAY

PATTERNS = settings().contacts["title_patterns"]


@pytest.mark.parametrize("title, kind", [
    ("HR Manager", "Recruiter/TA"),
    ("HR Business Partner", "Recruiter/TA"),
    ("Human Resources Specialist", "Recruiter/TA"),
    ("People Operations Lead", "Recruiter/TA"),
    ("Senior Talent Manager", "Recruiter/TA"),
    ("Specjalista ds. kadr", "Recruiter/TA"),
    ("Technical Recruiter", "Recruiter/TA"),
    ("Head of Platform", "Hiring"),
    ("Senior DevOps Engineer", "Peer engineer"),
    ("Site Reliability Engineer", "Peer engineer"),
    ("Thread engineer", "Other"),  # "hr" only as a whole word
])
def test_hr_titles_are_recruiters(title, kind):
    assert classify_title(title, PATTERNS) == kind


def test_engineers_get_the_referral_ask_and_hr_the_cold_mail():
    templates = fake_templates()
    kinds = {t: KIND[choose_template(templates, t, has_detail=True).template.key]
             for t in ("Peer engineer", "Recruiter/TA", "Hiring")}
    assert kinds == {"Peer engineer": "referral ask", "Recruiter/TA": "cold mail",
                     "Hiring": "cold mail"}
    # A generic mailbox (careers@, hr@) is never asked for a referral.
    assert KIND[choose_template(templates, "Other", True, generic=True).template.key] == \
        "cold mail"


def fresh_desk():
    return fake_desk(settings(), FAKE_TODAY, now=Clock())


def test_screening_sends_an_alert_card_per_apply_high_job():
    d = fresh_desk()
    replies = d.screen_replies()
    assert replies[0].text.startswith("Screening done:")
    alerts = replies[1:]
    highs = sorted(pid for pid, v in d.repo.rows.items()
                   if v.get("Screen verdict") == "Apply high" and v.get("Status") == "Screened")
    assert len(alerts) == len(highs) > 0
    first = alerts[0]
    assert first.text.startswith(HIGH_ALERT + "\n[1/")
    assert "Apply high\n" in first.text
    assert [b[1].split(":")[0] for b in first.buttons] == ["ap", "sk"]
    assert sorted(b[1].split(":", 1)[1] for a in alerts for b in a.buttons[:1]) == highs


def test_no_alert_for_jobs_already_handled_or_when_nothing_is_high():
    d = fresh_desk()
    d.screen()  # everything is screened now
    assert d.screen_replies()[1:] == []  # nothing new
    d = fresh_desk()
    _, summary = d._screen("", None)
    for result in summary.results:
        if result.verdict == "Apply high":
            d.repo.rows[result.page_id]["Status"] = "Approved"
    assert d.high_alerts(summary) == []


def test_alerts_are_capped(monkeypatch):
    monkeypatch.setattr(desk_module, "MAX_HIGH_ALERTS", 1)
    d = fresh_desk()
    replies = d.screen_replies()
    alerts = replies[1:]
    assert len(alerts) == 2
    assert alerts[0].text.startswith(HIGH_ALERT + "\n[1/1]")
    assert alerts[-1].text.endswith("more Apply high jobs are waiting: /pending")


def test_screen_command_sends_the_alerts_with_buttons():
    d = fresh_desk()
    fake = ButtonTelegram([[update(1, "/screen")]])
    tb.poll_once(tb.TelegramClient(fake), settings(), None, desk=d)
    texts = [t for _, t in fake.sent]
    assert any(t.startswith("[LOCAL] Screening done:") for t in texts)
    alerts = [i for i, t in enumerate(texts) if HIGH_ALERT in t]
    assert alerts
    assert all(fake.buttons[i][0].startswith("ap:") and fake.buttons[i][1].startswith("sk:")
               for i in alerts)
