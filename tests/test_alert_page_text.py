"""Job pages whose text is only in Next.js page data (Pracuj.pl, 7 Oct), and a log line when
an opened page gives no description."""

import json
import logging

from jobengine.sweep import fulltext
from jobengine.sweep.models import Job

DUTIES = "Maintain and improve our Kubernetes platform on AWS with Terraform and ArgoCD."
NEEDS = "At least 3 years of experience with Linux, CI/CD pipelines and Prometheus monitoring."
OFFER = "Hybrid work in Warsaw, private medical care, a budget for training and conferences."


def next_page(data):
    return ("<html><head><title>DevOps Engineer, Acme | Pracuj.pl</title></head><body>"
            "<div id='__next'></div>"
            f"<script id='__NEXT_DATA__' type='application/json'>{json.dumps(data)}</script>"
            "</body></html>")


PRACUJ_LIKE = {"props": {"pageProps": {"dehydratedState": {"queries": [{"state": {"data": {
    "jobTitle": "DevOps Engineer",
    "textSections": [
        {"sectionType": "responsibilities", "textElements": [DUTIES, DUTIES + " Daily."]},
        {"sectionType": "requirements-expected", "textElements": [NEEDS]},
        {"sectionType": "offered", "textElements": [f"<ul><li>{OFFER}</li></ul>"]},
    ],
    "trackingId": "abc123",
}}}]}}}}


def test_text_in_next_data_is_read():
    text = fulltext.extract(next_page(PRACUJ_LIKE))
    assert text is not None
    assert DUTIES in text and NEEDS in text and OFFER in text
    assert "abc123" not in text and "DevOps Engineer\n" not in text


def test_small_or_unrelated_next_data_gives_nothing():
    assert fulltext.from_next_data(fulltext.BeautifulSoup(next_page(
        {"props": {"pageProps": {"title": "Jobs", "description": "Find jobs"}}}),
        "html.parser")) is None


def test_an_opened_page_without_text_is_logged(caplog):
    job = Job(source="gmail", board="Pracuj.pl", company="Medicover", role="DevOps Engineer",
              city="Warszawa", country="Poland", url="https://links.grupapracuj.pl/c/1",
              posted_date=None, salary=None, seniority="Unknown", years_required=None,
              dedupe_key="k", posting_ref="gmail:1", description=None,
              description_is_snippet=True)
    page = "<html><head><title>Just a moment...</title></head><body>wait</body></html>"
    with caplog.at_level(logging.INFO, logger="jobengine.sweep"):
        result = fulltext.read(job, lambda url: ("https://www.pracuj.pl/praca/x,oferta,1", page))
    assert result is not None and result.url == "https://www.pracuj.pl/praca/x,oferta,1"
    assert result.description is None
    assert ("page read but no job description found for Medicover: www.pracuj.pl, "
            f"{len(page)} characters, title 'Just a moment...'") in caplog.text
