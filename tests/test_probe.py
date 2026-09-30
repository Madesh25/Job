"""FETCH 1: the job site probe measures and saves samples; it writes nothing to Notion."""

import json
from datetime import date

import httpx
import pytest

from jobengine import http
from jobengine.settings import load_settings
from jobengine.sweep import probe

S = load_settings("local", {})
TODAY = date(2026, 10, 1)


class Sites:
    """A fake web: url -> text, or an HttpError status."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        answer = self.pages.get(url, 404)
        if isinstance(answer, int):
            raise http.HttpError(f"GET {url} failed: HTTP {answer}", answer)
        return url, answer


def run(pages, names, tmp_path, titles=("DevOps Engineer",)):
    web = Sites(pages)
    report = probe.run(S, names, TODAY, get=web, out_dir=tmp_path / "out" / "probes",
                       pause=lambda seconds: None, titles=titles)
    return web, report


def test_json_answer_is_counted_and_saved(tmp_path):
    url = probe.search_url(probe.SITES["justjoin"], "DevOps Engineer")
    offers = {"data": [{"title": "DevOps", "publishedAt": "2026-09-30T10:00:00Z"},
                       {"title": "SRE", "publishedAt": "2026-09-29T08:00:00Z"},
                       {"title": "Old", "publishedAt": "2026-09-20T08:00:00Z"}]}
    web, report = run({url: json.dumps(offers)}, ["justjoin"], tmp_path)
    [result] = report.results
    assert (result.status, result.kind, result.recent_dates) == ("OK", "JSON", 2)
    saved = tmp_path / "out" / "probes" / "justjoin-devops-engineer.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == offers
    assert web.calls[-1][1]["accept_json"] is True
    assert "justjoin-devops-engineer.json" in report.text()


def test_page_data_json_is_taken_out_of_the_web_page(tmp_path):
    url = probe.search_url(probe.SITES["pracuj"], "DevOps Engineer")
    page = ('<html><script id="__NEXT_DATA__" type="application/json">'
            '{"props": {"offers": [{"lastPublicated": "2026-10-01T06:00:00"}]}}</script></html>')
    _, report = run({url: page}, ["pracuj"], tmp_path)
    [result] = report.results
    assert (result.kind, result.recent_dates) == ("page data JSON", 1)
    assert (tmp_path / "out" / "probes" / "pracuj-devops-engineer.json").exists()


def test_block_and_robots_are_reported_not_worked_around(tmp_path):
    iamexpat = probe.search_url(probe.SITES["iamexpat"], "DevOps Engineer")
    pages = {"https://www.irishjobs.ie/robots.txt": "User-agent: *\nAllow: /\n",
             "https://www.iamexpat.nl/robots.txt": "User-agent: *\nDisallow: /career/\n",
             iamexpat: "<p>never read</p>"}
    web, report = run(pages, ["irishjobs", "iamexpat"], tmp_path)
    irish, expat = report.results
    assert irish.status == "HTTP 404 (blocked or wrong address)"
    assert expat.status == "robots.txt disallows this search: skipped"
    assert iamexpat not in [url for url, _ in web.calls]


def test_eures_is_a_post_search_for_the_three_countries(tmp_path):
    url = probe.SITES["eures"].url
    web, _ = run({url: '{"jvs": []}'}, ["eures"], tmp_path)
    body = web.calls[-1][1]["json_body"]
    assert body["locationCodes"] == ["pl", "nl", "ie"] and body["sortSearch"] == "MOST_RECENT"
    assert body["keywords"][0]["keyword"] == "devops engineer"


def test_every_title_and_no_linkedin():
    assert not any("linkedin" in site.url for site in probe.SITES.values())
    assert len(probe.TITLES) == 4
    assert probe.recent_dates("2026-10-01 2026-09-29 2026-09-28 2026-13-01", TODAY) == 2


@pytest.fixture
def pages():
    calls, routes = [], {}

    def handler(request):
        calls.append(request)
        return routes[str(request.url)]

    http.set_transport(httpx.MockTransport(handler))
    yield calls, routes
    http.set_transport(None)


def test_get_page_reads_json_and_posts_only_when_asked(pages):
    calls, routes = pages
    routes["https://jobs.example.com/api"] = httpx.Response(
        200, json={"ok": True}, headers={"content-type": "application/json"})
    with pytest.raises(http.HttpError, match="not a web page"):
        http.get_page("https://jobs.example.com/api", s=S)
    _, text = http.get_page("https://jobs.example.com/api", s=S, accept_json=True,
                            json_body={"q": 1})
    assert json.loads(text) == {"ok": True}
    assert calls[-1].method == "POST" and json.loads(calls[-1].content) == {"q": 1}
    with pytest.raises(http.HttpError, match="linkedin is never fetched"):
        http.get_page("https://www.linkedin.com/jobs", s=S, accept_json=True)
