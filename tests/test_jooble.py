from datetime import date

import pytest

from jobengine import http
from jobengine.settings import load_settings
from jobengine.sweep.sources import jooble

S = load_settings("local", {"JOOBLE_API_KEY": "secret-key-123"})

JOBS = {
    "Ireland": [
        {"id": 101, "title": "<b>DevOps</b> Engineer", "company": "Liffey Analytics",
         "location": "Dublin", "snippet": "Run <b>Kubernetes</b> on AWS...",
         "salary": "EUR 60,000", "source": "irishjobs.ie",
         "link": "https://jooble.org/desc/101", "updated": "2026-09-27T00:00:00.0000000"},
        {"id": 102, "title": "Cloud Engineer", "company": "", "location": "",
         "snippet": "", "source": "example-board.com", "link": "https://jooble.org/desc/102",
         "updated": "bad"},
    ],
}


def fake_post(calls):
    def post(url, body):
        calls.append((url, body))
        return {"totalCount": 2, "jobs": JOBS.get(body["location"], [])}
    return post


def test_jooble_postings():
    calls = []
    result = jooble.fetch(S, post=fake_post(calls))
    first, second = result.postings
    assert (first.title, first.company, first.location_text) == (
        "DevOps Engineer", "Liffey Analytics", "Dublin, Ireland")
    assert first.board == "IrishJobs.ie"  # the site Jooble found it on
    assert first.posted_date == date(2026, 9, 27)
    assert first.salary_text == "EUR 60,000 (Jooble)"
    assert first.description_is_snippet and "Kubernetes" in first.description
    assert (second.company, second.location_text, second.board, second.posted_date) == (
        "(unknown)", "Ireland", "Other", None)
    # 3 countries x 4 terms, the same job twice is kept once
    assert len(calls) == 21 and len(result.postings) == 2
    assert calls[0][0] == "https://jooble.org/api/secret-key-123"
    assert calls[0][1] == {"keywords": "devops engineer", "location": "Ireland", "page": "1",
                           "ResultOnPage": "50"}


def test_jooble_skipped_without_key_or_when_disabled():
    no_key = load_settings("local", {})
    assert jooble.fetch(no_key).skipped_reason == "jooble skipped: JOOBLE_API_KEY missing"
    off = S.model_copy(update={"sweep": {**S.sweep, "jooble": {"enabled": False}}})
    assert jooble.fetch(off).skipped_reason == "jooble skipped: disabled in sweep.jooble"


def test_jooble_error_never_shows_the_key():
    def post(url, body):
        raise http.HttpError(f"POST {http.safe_url(url)} failed: HTTP 403", 403)

    result = jooble.fetch(S, post=post)
    assert result.notes == ["jooble Ireland: failed (HTTP 403)"]
    assert "secret-key-123" not in http.safe_url("https://jooble.org/api/secret-key-123")


@pytest.mark.parametrize("url", ["https://jooble.org/api/secret-key-123"])
def test_jooble_host_is_allowed(url):
    from jobengine.safety import assert_fetch_allowed
    assert assert_fetch_allowed(url, S) is None
