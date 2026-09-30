"""Email alerts: the Gmail label, the search and /alertcheck."""

from jobengine import gmail_reader
from jobengine.settings import load_settings
from jobengine.sweep import fakes, normalize
from jobengine.sweep.sources import gmail_alerts

S = load_settings("dev", {})
CFG = S.sweep["gmail"]
RULES = normalize.Rules.from_config(S.sweep)


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
                        "jobs in Europe: 3 job(s), 2 in scope")
    # Each dropped job says why.
    assert lines[2] == ("  dropped: Lead Site Reliability Engineer | Maas Logistics | Rotterdam, "
                        "South Holland, Netherlands: title excluded (lead)")
    # "Zobacz" is a button, not a job.
    assert ("- Other | oferty@grupapracuj.pl | Nowe oferty dla Ciebie: 1 job(s), 0 in scope"
            in lines)
    assert lines[-1].startswith("Jobs found in the emails shown: 7, in scope (title and "
                                "place): ")


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


LINKEDIN_CARD = """
<table><tr><td><a href="https://www.linkedin.com/comm/jobs/view/4099/?trk=eml">
<img src="https://media.example.com/logo.png" alt="co.brick"></a></td>
<td><a href="https://www.linkedin.com/comm/jobs/view/4099/?trk=eml">DevOps Engineer (AWS)</a>
<p>co.brick \u00b7 Warsaw, Mazowieckie, Poland (Hybrid)</p>
<p>Actively recruiting</p></td></tr></table>
"""


def test_linkedin_company_and_place_on_one_line():
    message = gmail_reader.GmailMessage(id="m", sender="x <jobalerts-noreply@linkedin.com>",
                                        subject="DevOps Engineer (AWS) at co.brick",
                                        html=LINKEDIN_CARD)
    [posting] = gmail_alerts.parse_alert(message, CFG)
    assert (posting.company, posting.location_text) == (
        "co.brick", "Warsaw, Mazowieckie, Poland (Hybrid)")
    job = normalize.normalize(posting, RULES, list(RULES.locations))
    assert (job.country, job.city, job.company) == ("Poland", "Warszawa", "co.brick")


JUSTJOIN_CARD = """
<div><a href="https://justjoin.it/job-offer/acme-kubernetes-engineer-krakow-devops">
Kubernetes Engineer</a><div>Acme Systems</div><div>Remote</div><div>25 000 PLN</div></div>
"""


def test_single_country_board_gives_its_country():
    message = gmail_reader.GmailMessage(id="m", sender="JustJoin.IT <no-reply@justjoin.it>",
                                        subject="New jobs for you", html=JUSTJOIN_CARD)
    [posting] = gmail_alerts.parse_alert(message, CFG)
    assert posting.location_area[-1] == "Poland"
    job = normalize.normalize(posting, RULES, list(RULES.locations))
    assert (job.country, job.city) == ("Poland", "Remote")
