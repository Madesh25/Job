import base64
from datetime import date

import pytest

from jobengine import gmail_reader, http
from jobengine.settings import load_settings
from jobengine.sweep import fakes
from jobengine.sweep.models import TargetCompany
from jobengine.sweep.normalize import Rules, detect_location, title_scope
from jobengine.sweep.sources import adzuna, ats, gmail_alerts

S = load_settings("local", {})
GMAIL_CFG = S.sweep["gmail"]
RULES = Rules.from_config(S.sweep)
ACTIVE = ["Poland", "Netherlands", "Ireland"]


def keep(title, location):
    country, _ = detect_location(location, RULES)
    return title_scope(title, RULES) is None and country in ACTIVE


def by_title(postings):
    return {p.title: p for p in postings}


# ---------------------------------------------------------------- Gmail


def test_board_for_sender():
    boards = GMAIL_CFG["sender_boards"]
    assert gmail_alerts.board_for_sender("x <jobalerts-noreply@linkedin.com>", boards) == "LinkedIn"
    assert gmail_alerts.board_for_sender("alerts@mail.irishjobs.ie", boards) == "IrishJobs.ie"
    assert gmail_alerts.board_for_sender("alerts@jobs.ie", boards) == "Jobs.ie"
    assert gmail_alerts.board_for_sender("someone@unknown.example", boards) == "Other"


def test_linkedin_alert_parsed_from_cards():
    message = fakes.gmail_messages()[0]
    postings = by_title(gmail_alerts.parse_alert(message, GMAIL_CFG))
    assert set(postings) == {
        "Senior DevOps Engineer",
        "Platform Engineer (m/f/d)",
        "Lead Site Reliability Engineer",
    }
    senior = postings["Senior DevOps Engineer"]
    assert senior.board == "LinkedIn"
    assert senior.company == "Northwind Cloud"
    assert senior.location_text == "Dublin, County Dublin, Ireland"
    assert senior.posting_id == "4012345678"
    assert senior.url.startswith("https://www.linkedin.com/comm/jobs/view/4012345678/")
    assert senior.description is None and senior.posted_date is None


def test_justjoin_alert_keeps_duplicate_link_once_and_polish_city():
    message = fakes.gmail_messages()[1]
    postings = gmail_alerts.parse_alert(message, GMAIL_CFG)
    assert [p.title for p in postings] == ["DevOps Engineer", "Mid Cloud Engineer"]
    assert postings[0].posting_id == "vistula-cloud-devops-engineer-krakow-devops"
    assert postings[1].location_text == "Wrocław"
    assert postings[1].company == "Odra Systems S.A."


def test_generic_alert_keeps_tracking_link_without_requesting_it(monkeypatch):
    def no_requests(*args, **kwargs):
        raise AssertionError("alert links must never be requested")

    monkeypatch.setattr(http, "request_json", no_requests)
    message = fakes.gmail_messages()[2]
    postings = gmail_alerts.parse_alert(message, GMAIL_CFG)
    assert len(postings) == 1
    posting = postings[0]
    assert posting.board == "IrishJobs.ie"
    assert posting.title == "Site Reliability Engineer"
    assert posting.company == "Liffey Analytics Ltd"
    assert posting.location_text == "Cork, Ireland"
    assert posting.url == "https://click.mailtrack.example.com/ls/click?upn=abc123def456&u=987"
    assert posting.posting_id == gmail_alerts.fallback_posting_id(
        "IrishJobs.ie", "Site Reliability Engineer", "Liffey Analytics Ltd"
    )
    assert len(posting.posting_id) == 16


def test_card_heuristic_falls_back_to_unknown():
    message = gmail_reader.GmailMessage(
        id="m", sender="a@example.com", subject="s",
        html="<p><a href='https://example.com/job/1'>DevOps Engineer at a very long "
             "sentence</a> " + ("filler text " * 30) + "</p>",
    )
    [posting] = gmail_alerts.parse_alert(message, GMAIL_CFG)
    assert posting.company == "(unknown)" and posting.location_text == ""


def test_same_link_across_messages_kept_once():
    message = fakes.gmail_messages()[0]
    postings = gmail_alerts.collect([message, message], GMAIL_CFG)
    assert len(postings) == 3


def test_gmail_fetch_skips_without_token():
    result = gmail_alerts.fetch(S)
    assert result.skipped_reason == "gmail skipped: GMAIL_ALERTS_TOKEN_JSON missing"


def test_gmail_fetch_with_fake_loader():
    result = gmail_alerts.fetch(S, load_messages=fakes.gmail_messages)
    assert len(result.postings) == 6


def test_gmail_reader_decodes_html_part():
    html = "<p>hello</p>"
    data = base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")
    raw = {
        "id": "m1",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": "a@justjoin.it"},
                        {"name": "Subject", "value": "Hi"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "aGk"}},
                {"mimeType": "text/html", "body": {"data": data}},
            ],
        },
    }
    message = gmail_reader.to_message(raw)
    assert message.html == html and message.sender == "a@justjoin.it"


def test_gmail_reader_uses_list_and_get_only():
    calls = []

    class Call:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class Messages:
        def list(self, **kw):
            calls.append(("list", kw))
            return Call({"messages": [{"id": "m1"}]})

        def get(self, **kw):
            calls.append(("get", kw))
            return Call({"id": "m1", "payload": {"mimeType": "text/html",
                                                "body": {"data": "PHA-aGk8L3A-"}}})

    class Users:
        def messages(self):
            return Messages()

    class Service:
        def users(self):
            return Users()

    messages = gmail_reader.fetch_messages(Service(), "newer_than:2d", 10)
    assert [name for name, _ in calls] == ["list", "get"]
    assert calls[1][1]["format"] == "full"
    assert len(messages) == 1


# ---------------------------------------------------------------- Adzuna


def test_adzuna_fields_and_predicted_salary_not_stored():
    http_fake = fakes.FixtureHttp(S)
    result = adzuna.fetch(S, get=http_fake)
    postings = by_title(result.postings)
    assert len(result.postings) == 7
    first = postings["DevOps Engineer"]  # <strong> tags stripped
    assert first.board == "Adzuna" and first.posting_id == "4300000001"
    assert first.company == "Vistula Cloud Sp. z o.o."
    assert first.location_area == ("Polska", "Małopolskie", "Kraków")
    assert first.posted_date == date(2026, 9, 29)
    assert first.salary_text == "18000 - 24000 PLN (Adzuna)"
    assert first.description_is_snippet is True
    assert postings["Senior Site Reliability Engineer"].salary_text is None  # predicted
    assert postings["Cloud Engineer"].salary_text == "4500 - 6000 EUR (Adzuna)"
    assert http_fake.requested == [
        "https://api.adzuna.com/v1/api/jobs/pl/search/1",
        "https://api.adzuna.com/v1/api/jobs/nl/search/1",
    ]


def test_adzuna_skipped_without_keys():
    result = adzuna.fetch(S)
    assert result.skipped_reason == "adzuna skipped: ADZUNA_APP_ID or ADZUNA_APP_KEY missing"
    assert result.postings == []


def test_adzuna_respects_call_limit_and_params():
    calls = []

    def get(url, params):
        calls.append((url, dict(params)))
        return {"results": [{"id": str(len(calls)), "title": "SRE"}] * 50}

    s = load_settings("local", {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"})
    result = adzuna.fetch(s, get=get)
    assert len(calls) == 6
    assert calls[0][1] == {
        "app_id": "id", "app_key": "key", "results_per_page": 50, "max_days_old": 3,
        "what_or": "devops sre kubernetes platform cloud infrastructure",
        "content-type": "application/json",
    }
    assert calls[1][0].endswith("/pl/search/2")
    assert "adzuna: stopped at the limit of 6 calls" in result.notes


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"salary_min": 5000, "salary_max": 5000, "salary_is_predicted": "0"}, "5000 EUR (Adzuna)"),
        ({"salary_min": 5000, "salary_is_predicted": 1}, None),
        ({"salary_is_predicted": "0"}, None),
    ],
)
def test_adzuna_salary_text(result, expected):
    assert adzuna.salary_text(result, "EUR") == expected


# ---------------------------------------------------------------- ATS


def company(name, url, platform="Unknown", active=True):
    return TargetCompany(name=name, careers_url=url, ats_platform=platform, active=active)


def test_detect_board():
    assert ats.detect_board(company("A", "https://boards.greenhouse.io/acme"), {}) == (
        ats.Board("greenhouse", "acme")
    )
    assert ats.detect_board(company("A", "https://job-boards.eu.greenhouse.io/acme-eu"), {}) == (
        ats.Board("greenhouse", "acme-eu")
    )
    assert ats.detect_board(
        company("A", "https://boards.greenhouse.io/embed/job_board?for=acme"), {}
    ) == ats.Board("greenhouse", "acme")
    assert ats.detect_board(company("A", "https://jobs.eu.lever.co/tulip"), {}) == (
        ats.Board("lever", "tulip", eu=True)
    )
    assert ats.detect_board(company("A", "https://jobs.lever.co/tulip"), {}) == (
        ats.Board("lever", "tulip")
    )
    assert ats.detect_board(company("A", "https://careers.smartrecruiters.com/Acme1"), {}) == (
        ats.Board("smartrecruiters", "Acme1")
    )
    assert ats.detect_board(company("A", "https://acme.wd3.myworkdayjobs.com/x"), {}) is None


def test_ats_board_override_for_company_domain():
    shamrock = company("Shamrock Systems", "https://careers.shamrocksystems.example.com/jobs")
    overrides = {"Shamrock Systems": {"ats": "Greenhouse", "token": "shamrock"}}
    assert ats.detect_board(shamrock, overrides) == ats.Board("greenhouse", "shamrock")


def test_ats_fetch_with_fixtures():
    http_fake = fakes.FixtureHttp(S)
    result = ats.fetch(S, fakes.target_companies(), keep, get=http_fake)
    postings = by_title(result.postings)
    assert set(postings) == {
        "Senior DevOps Engineer", "Site Reliability Engineer", "Platform Engineer",
        "DevOps Engineer",
    }
    assert result.not_supported == ["Baltic Bank", "Shamrock Systems"]
    assert all(p.board == "Company site" and p.source == "ats" for p in result.postings)
    assert "ats: 4 postings outside title or location scope were not fetched" in result.notes
    # Detail is requested only for the SmartRecruiters posting that passed the filters.
    details = [u for u in http_fake.requested if "smartrecruiters" in u and "/postings/" in u]
    assert details == [
        "https://api.smartrecruiters.com/v1/companies/VistulaPayments/postings/744000011111111"
    ]
    assert "https://api.eu.lever.co/v0/postings/tulipdata" in http_fake.requested


def test_greenhouse_uses_first_published_never_updated_at():
    result = ats.fetch(S, fakes.target_companies()[:1], keep, get=fakes.FixtureHttp(S))
    postings = by_title(result.postings)
    assert postings["Senior DevOps Engineer"].posted_date == date(2026, 9, 10)
    assert postings["Site Reliability Engineer"].posted_date is None  # only updated_at given
    description = postings["Senior DevOps Engineer"].description
    assert "4+ years of experience with AWS" in description and "&lt;" not in description


def test_lever_and_smartrecruiters_fields():
    result = ats.fetch(S, fakes.target_companies()[1:3], keep, get=fakes.FixtureHttp(S))
    postings = by_title(result.postings)
    lever_job = postings["Platform Engineer"]
    assert lever_job.posted_date == date(2026, 9, 24)
    assert lever_job.salary_text == "60000 - 75000 EUR per year salary (Lever)"
    assert "3+ years operating Kubernetes" in lever_job.description
    sr_job = postings["DevOps Engineer"]
    assert sr_job.posted_date == date(2026, 9, 25)
    assert sr_job.location_text == "Warszawa, Mazowieckie, Poland"
    assert sr_job.url.endswith("744000011111111-devops-engineer")
    assert "min. 3 lata" in sr_job.description


def test_smartrecruiters_detail_budget():
    s = load_settings("local", {}).model_copy(
        update={"sweep": {**S.sweep, "ats": {"max_detail_calls": 0}}}
    )
    http_fake = fakes.FixtureHttp(s)
    result = ats.fetch(s, fakes.target_companies()[2:3], keep, get=http_fake)
    assert [p.description for p in result.postings] == [None]
    assert not [u for u in http_fake.requested if "/postings/" in u]


def test_ats_error_is_noted_and_other_companies_continue():
    def get(url, params):
        if "greenhouse" in url:
            raise http.HttpError("GET https://boards-api.greenhouse.io/v1/boards/x/jobs failed: "
                                 "HTTP 404", 404)
        return fakes.FixtureHttp(S)(url, params)

    result = ats.fetch(S, fakes.target_companies()[:2], keep, get=get)
    assert [p.title for p in result.postings] == ["Platform Engineer"]
    assert any(note.startswith("ats Northwind Cloud:") for note in result.notes)


def test_inactive_companies_are_ignored():
    inactive = [company("A", "https://boards.greenhouse.io/acme", active=False)]
    result = ats.fetch(S, inactive, keep, get=fakes.FixtureHttp(S))
    assert result.postings == [] and result.not_supported == []
