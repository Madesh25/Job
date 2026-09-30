"""Email alerts: the Gmail label, the search and /alertcheck."""

from jobengine import gmail_reader
from jobengine.settings import load_settings
from jobengine.sweep import fakes
from jobengine.sweep.sources import gmail_alerts

S = load_settings("dev", {})
CFG = S.sweep["gmail"]


def test_label_term():
    assert gmail_alerts.label_term("Job Alerts") == "label:job-alerts"
    assert gmail_alerts.label_term("Jobs/Poland") == "label:jobs-poland"


def test_query_reads_the_label_or_a_known_sender():
    query = gmail_alerts.alert_query(CFG)
    assert query.startswith("newer_than:2d -in:spam -in:trash (label:job-alerts OR from:(")
    assert "linkedin.com OR justjoin.it OR" in query and "pracuj.pl" in query
    assert gmail_alerts.alert_query({"query": "from:x.example"}) == "from:x.example"
    assert gmail_alerts.alert_query({"days": 1}) == "newer_than:1d -in:spam -in:trash"


def test_fetch_counts_the_emails():
    result = gmail_alerts.fetch(S, load_messages=fakes.gmail_messages)
    assert result.emails == 3 and len(result.postings) == 6


def test_a_failure_keeps_its_reason():
    def broken():
        raise RuntimeError("invalid_grant: Token has been expired or revoked.")

    result = gmail_alerts.fetch(S, load_messages=broken)
    assert result.skipped_reason == ("gmail failed: RuntimeError: invalid_grant: Token has been "
                                     "expired or revoked.")


def test_alertcheck_lists_each_email_and_why_one_gave_nothing():
    odd = gmail_reader.GmailMessage(
        id="m", sender="Pracuj.pl <oferty@grupapracuj.pl>", subject="Nowe oferty dla Ciebie",
        html='<a href="https://links.grupapracuj.pl/c/1">Zobacz</a>'
             '<a href="https://links.grupapracuj.pl/c/2">DevOps Engineer</a>')
    text = gmail_alerts.check(S, lambda: [*fakes.gmail_messages(), odd])
    lines = text.splitlines()
    assert lines[0].startswith("4 alert email(s) found (search: newer_than:2d")
    assert lines[1] == ("- LinkedIn | jobalerts-noreply@linkedin.com | DevOps Engineer: 3 new "
                        "jobs in Europe: 3 job(s)")
    # "Zobacz" is a button, not a job.
    assert "- Other | oferty@grupapracuj.pl | Nowe oferty dla Ciebie: 1 job(s)" in lines
    assert lines[-1] == "Jobs found in the emails shown: 7."


def test_alertcheck_explains_an_empty_result_and_a_missing_token():
    assert gmail_alerts.check(S, lambda: []).startswith(
        "No alert email found. Gmail search used:\nnewer_than:2d")
    assert gmail_alerts.check(load_settings("dev", {"GMAIL_ALERTS_TOKEN_JSON": ""})).startswith(
        "GMAIL_ALERTS_TOKEN_JSON is not set")


def test_no_job_link_shows_where_the_links_go():
    empty = gmail_reader.GmailMessage(
        id="m", sender="x <a@theprotocol.it>", subject="Oferty",
        html='<a href="https://links.theprotocol.it/c/1">Zobacz</a>'
             '<a href="https://links.theprotocol.it/c/2">Ustawienia</a>')
    text = gmail_alerts.check(S, lambda: [empty])
    assert "- theprotocol.it | a@theprotocol.it | Oferty: 0 job(s)" in text
    assert "  no job link recognised; links go to: links.theprotocol.it" in text


def test_alertcheck_in_the_bot():
    from test_telegram_bot import Clock, settings

    from jobengine import telegram_bot as tb
    from jobengine.screen.desk import fake_desk
    from jobengine.sweep.fakes import FAKE_TODAY

    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    assert d.alertcheck_command()[0].text.startswith("3 alert email(s) found")
    assert "alertcheck" in {c["command"] for c in tb.bot_commands()}
