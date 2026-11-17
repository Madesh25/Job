"""IamExpat reader (FETCH 7), built from the probe sample of 5 Oct. The page below has the
same shape as the real IT jobs page (Next.js page data plus job card links), with made-up
jobs."""

import json
from datetime import date

from jobengine import http
from jobengine.settings import load_settings
from jobengine.sweep.normalize import Rules, Skipped, normalize
from jobengine.sweep.sources import iamexpat

S = load_settings("local", {})
RULES = Rules.from_config(S.sweep)
ACTIVE = ["Poland", "Netherlands", "Ireland"]
PATH = iamexpat.IT_PATH


def ad(title, company, city, uuid, posted="2026-10-01T09:00:00", salary=None, about="$a6"):
    return {"JobTitle": title, "id": uuid, "AboutThisRole": about, "Requirements": "",
            "Salary": salary, "Remote": False, "PostedDate": posted,
            "JobProvider": {"CompanyName": company}, "Location": {"Title": city}}


ADS = [ad("Platform Engineer", "Acme Cloud", "Amsterdam", "e0e5efc0-5501-436a-9745-c680891167b8",
          salary="5000 - 6500 \n\nFull-time, hybrid in Amsterdam.",
          about="Run our Kubernetes platform on AWS."),
       ad("iOS Developer", "Bunq", "Amsterdam", "931c2745-0000-4000-8000-000000000001"),
       ad("Cloud Engineer", "No Link BV", "Utrecht", "11111111-2222-4333-8444-555555555555")]


def page(ads, linked=2):
    flight = '22:["$","$L9a",null,{"initialNbPages":1,"initialJobAds":' + json.dumps(ads) + "}]"
    half = len(flight) // 2  # the data is split over several chunks on the real page
    chunks = "".join(f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>"
                     for part in (flight[:half], flight[half:]))
    cards = "".join(f'<a href="{PATH}/job-{n}/{iamexpat.short_id(a["id"])}">x</a>'
                    for n, a in enumerate(ads[:linked]))
    return f"<html><body>{cards}{chunks}</body></html>"


def test_the_link_id_is_the_uuid_in_base_58():
    # Seen on the real page: this uuid's job link ends in tLJWUBCWY1P8MBXMScbwRE.
    assert iamexpat.short_id("e0e5efc0-5501-436a-9745-c680891167b8") == "tLJWUBCWY1P8MBXMScbwRE"
    assert iamexpat.short_id("not a uuid") == ""


def test_jobs_are_read_from_the_page_data():
    text = page(ADS)
    assert [a["JobTitle"] for a in iamexpat.ads_from_page(text)] == [a["JobTitle"] for a in ADS]
    assert iamexpat.ads_from_page("<html>no data</html>") == []


def test_a_posting_has_company_place_date_salary_and_the_job_link():
    text = page(ADS)
    links = iamexpat.links_by_id(text)
    p = iamexpat.to_posting(ADS[0], links)
    assert (p.title, p.company, p.location_text) == ("Platform Engineer", "Acme Cloud",
                                                     "Amsterdam, Netherlands")
    assert p.url == f"https://www.iamexpat.nl{PATH}/job-0/tLJWUBCWY1P8MBXMScbwRE"
    assert p.posted_date == date(2026, 10, 1) and p.board == "IamExpat"
    assert p.salary_text == "5000 - 6500 EUR per month (IamExpat)"
    assert p.description == "Run our Kubernetes platform on AWS."
    other = iamexpat.to_posting(ADS[1], links)
    assert other.description is None and other.salary_text is None  # "$a6" is a reference
    assert iamexpat.to_posting(ADS[2], links) is None  # no job card link on the page
    kept = [normalize(iamexpat.to_posting(a, links), RULES, ACTIVE) for a in ADS[:2]]
    assert not isinstance(kept[0], Skipped)
    assert isinstance(kept[1], Skipped)  # iOS is not a target role


def test_fetch_reads_one_page_and_stops_quietly_when_blocked():
    asked = []

    def get(url):
        asked.append(url)
        if url.endswith("/robots.txt"):
            return url, "User-agent: *\nDisallow: /api/"
        return url, page(ADS)

    result = iamexpat.fetch(S, get=get, pause=lambda _: None)
    assert len(result.postings) == 2 and asked[1] == iamexpat.LIST_URL and len(asked) == 2

    def blocked(url):
        if url.endswith("/robots.txt"):
            return url, ""
        raise http.HttpError("GET failed: HTTP 403", 403)

    result = iamexpat.fetch(S, get=blocked, pause=lambda _: None)
    assert result.postings == [] and "HTTP 403" in result.notes[0]

    result = iamexpat.fetch(S, get=lambda url: (url, "<html>new layout</html>"),
                            pause=lambda _: None)
    assert "layout may have changed" in result.notes[0]


def test_robots_txt_that_forbids_the_page_is_respected():
    def get(url):
        if url.endswith("/robots.txt"):
            return url, "User-agent: *\nDisallow: /career/"
        raise AssertionError("the page must not be requested")

    result = iamexpat.fetch(S, get=get, pause=lambda _: None)
    assert "robots.txt" in result.notes[0]

