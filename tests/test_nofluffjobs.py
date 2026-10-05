"""NoFluffJobs reader (FETCH 4), built from the probe samples of 5 Oct. The page below has
the same shape as the real search page, with made-up jobs."""

import json
from datetime import date

from jobengine import http
from jobengine.settings import load_settings
from jobengine.sweep.normalize import Rules, Skipped, normalize
from jobengine.sweep.sources import nofluffjobs

S = load_settings("local", {})
RULES = Rules.from_config(S.sweep)
ACTIVE = ["Poland", "Netherlands", "Ireland"]
SEPT_30 = 1790805677430  # 2026-09-30 in milliseconds


def job(title, company, city, slug, posted=SEPT_30, country="Poland", salary=None, remote=False):
    return {"id": slug, "url": slug, "reference": slug.upper()[:8], "title": title,
            "name": company, "posted": posted, "fullyRemote": remote, "salary": salary,
            "location": {"places": [{"city": city, "country": {"code": "X", "name": country},
                                     "url": slug}], "fullyRemote": remote}}


def page(postings):
    state = {"STORE_KEY": {"searchResponse": {"postings": postings, "totalCount": 2}}}
    raw = json.dumps(state).replace('"', "&q;")
    return ('<html><body><script id="serverApp-state" type="application/json">'
            f"{raw}</script></body></html>")


JOBS = [job("Site Reliability Engineer", "Acme Cloud", "Kraków", "sre-acme-krakow",
            salary={"from": 20000, "to": 26000, "type": "b2b", "currency": "PLN",
                    "period": "Month"}),
        job("Platform Engineer", "Northwind", "Budapest", "platform-northwind-budapest",
            country="Hungary"),
        job("AI Engineer", "Odra", "Warszawa", "ai-odra-warszawa")]


def test_postings_are_read_from_the_page_data():
    found = nofluffjobs.postings_from_page(page(JOBS))
    assert [j["title"] for j in found] == [j["title"] for j in JOBS]
    assert nofluffjobs.postings_from_page("<html>no data</html>") == []


def test_a_posting_has_company_place_date_salary_and_the_job_link():
    p = nofluffjobs.to_posting(JOBS[0])
    assert (p.title, p.company, p.location_text) == ("Site Reliability Engineer", "Acme Cloud",
                                                     "Kraków, Poland")
    assert p.url == "https://nofluffjobs.com/pl/job/sre-acme-krakow"
    assert p.posted_date == date(2026, 9, 30) and p.board == "NoFluffJobs"
    assert p.salary_text == "20000 - 26000 PLN per month, b2b (NoFluffJobs)"
    assert p.description is None  # read from the job page by fulltext
    kept = [normalize(nofluffjobs.to_posting(j), RULES, ACTIVE) for j in JOBS]
    assert not isinstance(kept[0], Skipped)
    assert isinstance(kept[1], Skipped) and isinstance(kept[2], Skipped)  # Hungary, AI title


def test_fetch_reads_each_term_once_and_stops_quietly_when_blocked():
    asked = []

    def get(url):
        asked.append(url)
        if url.endswith("/robots.txt"):
            return url, "User-agent: *\nDisallow: /api/"
        return url, page(JOBS)

    cfg = {**S.sweep, "nofluffjobs": {"search_terms": ["devops engineer", "sre"], "pages": 1}}
    s = S.model_copy(update={"sweep": cfg})
    result = nofluffjobs.fetch(s, get=get, pause=lambda _: None)
    assert len(result.postings) == 3  # the same jobs on both searches count once
    assert asked[1].startswith("https://nofluffjobs.com/pl/?criteria=keyword%3Ddevops+engineer")

    def blocked(url):
        if url.endswith("/robots.txt"):
            return url, ""
        raise http.HttpError("GET failed: HTTP 403", 403)

    result = nofluffjobs.fetch(s, get=blocked, pause=lambda _: None)
    assert result.postings == [] and "HTTP 403" in result.notes[0]


def test_robots_txt_that_forbids_the_search_is_respected():
    def get(url):
        if url.endswith("/robots.txt"):
            return url, "User-agent: *\nDisallow: /pl/"
        raise AssertionError("the search must not be requested")

    result = nofluffjobs.fetch(S, get=get, pause=lambda _: None)
    assert "robots.txt" in result.notes[0]
