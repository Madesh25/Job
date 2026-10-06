"""Phase 4 fixes (6 Oct): slow buttons are reported, alert jobs carry the email date, a job
board shared by several Target Companies rows is read once, own-site companies are counted
once."""

import time
from datetime import date

from jobengine import gmail_reader
from jobengine import telegram_bot as tb
from jobengine.settings import load_settings
from jobengine.sweep.models import SourceResult, TargetCompany
from jobengine.sweep.sources import ats, gmail_alerts

S = load_settings("local", {})
TODAY = date(2026, 10, 6)


class Sent:
    def __init__(self):
        self.sent = []

    def __call__(self, method, payload, timeout):
        if method == "sendMessage":
            self.sent.append(payload["text"])
        return {"ok": True, "result": {}}


def test_a_slow_button_is_reported_once_and_a_fast_one_never(capfd):
    fake = Sent()
    client = tb.TelegramClient(fake)
    with tb.SlowWatch(client, "1", "ra:abc", seconds=0.05):
        time.sleep(0.3)
    assert len(fake.sent) == 1 and fake.sent[0].startswith("Still working on it")
    assert "Thread" in capfd.readouterr().err  # the stacks went to the log
    with tb.SlowWatch(client, "1", "sk:abc", seconds=5):
        pass
    time.sleep(0.05)
    assert len(fake.sent) == 1


def test_alert_jobs_carry_the_email_date():
    raw = {"id": "m1", "internalDate": "1791262800000",  # 2026-10-06 UTC
           "payload": {"mimeType": "text/html", "headers": [
               {"name": "From", "value": "LinkedIn <jobalerts-noreply@linkedin.com>"},
               {"name": "Subject", "value": "jobs"}],
               "body": {"data": "PGh0bWw-PC9odG1sPg"}}}
    message = gmail_reader.to_message(raw)
    assert message.received == TODAY
    html = ('<a href="https://www.linkedin.com/comm/jobs/view/123">DevOps Engineer</a>'
            "<div>Acme</div><div>Dublin</div>")
    message = gmail_reader.GmailMessage("m1", message.sender, "jobs", f"<div>{html}</div>",
                                        received=TODAY)
    postings = gmail_alerts.parse_alert(message, S.sweep["gmail"])
    assert postings and all(p.posted_date == TODAY for p in postings)


def test_a_shared_board_is_read_once_and_named_without_the_region():
    link = "https://jobs.ashbyhq.com/capgemini"
    companies = [TargetCompany("Capgemini", link, "Custom", True),
                 TargetCompany("Capgemini (IE)", link, "Custom", True),
                 TargetCompany("Capgemini (NL)", link, "Custom", True)]
    reads = []

    def get(url, params=None):
        reads.append(url)
        return {"jobs": [{"id": "1", "title": "DevOps Engineer", "location": "Dublin",
                          "isListed": True}]}

    result = ats.fetch(S, companies, lambda t, loc: True, get=get, post=lambda u, b: {},
                       page=lambda u: (u, ""), today=TODAY)
    assert len(reads) == 1
    assert [p.company for p in result.postings] == ["Capgemini"]
    region_only = ats.fetch(S, companies[1:], lambda t, loc: True, get=get,
                            post=lambda u, b: {}, page=lambda u: (u, ""), today=TODAY)
    assert [p.company for p in region_only.postings] == ["Capgemini"]


def test_own_site_companies_are_counted_once_per_company():
    from jobengine.sweep.runner import fake_deps, run_sweep

    deps = fake_deps(S)
    real_ats = deps.ats

    def ats_with_own_sites(*args, **kwargs):
        result = real_ats(*args, **kwargs)
        return SourceResult(name="ats", postings=result.postings,
                            own_site=["Infosys", "Infosys (IE)", "Infosys (NL)", "Google"])

    deps.ats = ats_with_own_sites
    summary = run_sweep(S, deps, TODAY)
    assert summary.own_site_count == 2
