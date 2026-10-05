import json
from dataclasses import replace
from datetime import date

import httpx
import pytest

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.safety import SafetyError, assert_fetch_allowed, assert_page_fetch_allowed
from jobengine.settings import load_settings
from jobengine.sweep import fakes, fulltext
from jobengine.sweep.dedupe import description_blocks
from jobengine.sweep.models import Job, TargetCompany
from jobengine.sweep.normalize import Rules, detect_location, title_scope
from jobengine.sweep.runner import fake_deps, run_sweep
from jobengine.sweep.sources import ats

S = load_settings("local", {})
RULES = Rules.from_config(S.sweep)
TODAY = date(2026, 10, 1)
CFG = fulltext.Config()

JOB = Job(
    source="adzuna", board="Adzuna", company="Example Hosting", role="DevOps Engineer",
    city="Krakow", country="Poland", url="https://www.adzuna.pl/details/1", posted_date=None,
    salary=None, seniority="Unknown", years_required=None, dedupe_key="k",
    posting_ref="adzuna:1", description="We run services.", description_is_snippet=True,
)
LONG = " ".join(["Operate Kubernetes and Terraform on AWS with 3+ years of experience."] * 12)


def keep(title, location):
    country, _ = detect_location(location, RULES)
    return title_scope(title, RULES) is None and country in ("Poland", "Netherlands", "Ireland")


# ---------------------------------------------------------------- page safety


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/1",
    "http://careers.example.com/job/1",
    "https://127.0.0.1/job",
    "https://[::1]/job",
    "https://localhost/job",
    "https://metadata.google.internal/computeMetadata",
    "https://intranet/job",
    "https://user:pw@careers.example.com/job",
])
def test_page_reader_refuses_unsafe_urls(url):
    with pytest.raises(SafetyError):
        assert_page_fetch_allowed(url, S)


def test_page_reader_allows_public_https_pages():
    assert assert_page_fetch_allowed("https://careers.example.com/jobs/1?ref=adzuna", S) is None


def test_workday_hosts_are_allowed_by_suffix_only():
    assert_fetch_allowed("https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/x/jobs", S)
    assert_fetch_allowed("https://www.amazon.jobs/en/search.json", S)
    with pytest.raises(SafetyError):
        assert_fetch_allowed("https://myworkdayjobs.com.evil.example.com/x", S)


@pytest.fixture
def pages():
    calls = []
    routes = {}

    def handler(request):
        calls.append(request)
        return routes[str(request.url)]

    http.set_transport(httpx.MockTransport(handler))
    yield calls, routes
    http.set_transport(None)


def html_response(text, **kw):
    return httpx.Response(200, text=text, headers={"content-type": "text/html; charset=utf-8"},
                          **kw)


def test_get_page_follows_checked_redirects(pages):
    calls, routes = pages
    routes["https://www.adzuna.pl/land/ad/1"] = httpx.Response(
        302, headers={"location": "https://careers.example.com/job/1", "set-cookie": "a=b"})
    routes["https://careers.example.com/job/1"] = html_response("<p>ok</p>")
    final, text = http.get_page("https://www.adzuna.pl/land/ad/1", s=S)
    assert final == "https://careers.example.com/job/1" and text == "<p>ok</p>"
    assert "cookie" not in calls[1].headers  # no cookies are carried between hops
    assert "JobEngine" in calls[0].headers["user-agent"]


def test_get_page_without_charset_or_with_a_bad_one(pages):
    _, routes = pages
    routes["https://jobs.example.com/a"] = httpx.Response(
        200, content="Kraków".encode(), headers={"content-type": "text/html"})
    routes["https://jobs.example.com/b"] = httpx.Response(
        200, content=b"ok", headers={"content-type": "text/html; charset=nonsense"})
    assert http.get_page("https://jobs.example.com/a", s=S)[1] == "Kraków"
    assert http.get_page("https://jobs.example.com/b", s=S)[1] == "ok"


def test_get_page_refuses_a_redirect_to_linkedin(pages):
    calls, routes = pages
    routes["https://jobs.example.com/r/1"] = httpx.Response(
        301, headers={"location": "https://www.linkedin.com/jobs/view/9"})
    with pytest.raises(http.HttpError, match="linkedin"):
        http.get_page("https://jobs.example.com/r/1", s=S)
    assert len(calls) == 1  # linkedin itself is never requested


def test_get_page_limits_size_type_and_redirects(pages):
    _, routes = pages
    routes["https://jobs.example.com/big"] = html_response("x" * 5000)
    with pytest.raises(http.HttpError, match="larger than"):
        http.get_page("https://jobs.example.com/big", s=S, max_bytes=1000)
    routes["https://jobs.example.com/file"] = httpx.Response(
        200, content=b"%PDF", headers={"content-type": "application/pdf"})
    with pytest.raises(http.HttpError, match="not a web page"):
        http.get_page("https://jobs.example.com/file", s=S)
    routes["https://jobs.example.com/loop"] = httpx.Response(
        302, headers={"location": "https://jobs.example.com/loop"})
    with pytest.raises(http.HttpError, match="redirects"):
        http.get_page("https://jobs.example.com/loop", s=S)
    routes["https://jobs.example.com/gone"] = httpx.Response(404)
    with pytest.raises(http.HttpError, match="HTTP 404"):
        http.get_page("https://jobs.example.com/gone", s=S)


# ---------------------------------------------------------------- extraction


def test_extract_prefers_the_json_ld_job_posting():
    posting = {"@context": "https://schema.org", "@type": ["JobPosting"],
               "description": f"<p>{LONG}</p><ul><li>Docker</li></ul>"}
    page = (f'<script type="application/ld+json">{json.dumps([posting])}</script>'
            "<main>Short teaser only</main>")
    text = fulltext.extract(page)
    assert text.startswith("Operate Kubernetes") and text.endswith("Docker")


def test_extract_uses_the_description_block_without_json_ld():
    page = (f"<html><body><nav>{'Menu ' * 80}</nav>"
            f"<div id='job-description'><h2>About</h2><p>{LONG}</p></div>"
            "<footer>Cookies</footer></body></html>")
    text = fulltext.extract(page)
    assert text.startswith("About\nOperate Kubernetes")
    assert "Menu" not in text and "Cookies" not in text


def test_extract_gives_none_for_a_page_without_text():
    assert fulltext.extract("<html><body><p>Apply now</p></body></html>") is None


def test_needs_text_rules():
    assert fulltext.needs_text(JOB, CFG)
    assert not fulltext.needs_text(replace(JOB, description=LONG, description_is_snippet=False),
                                   CFG)
    assert fulltext.needs_text(replace(JOB, source="ats", description=None), CFG)
    # Alert email links are followed to the real job page; LinkedIn never.
    assert fulltext.needs_text(replace(JOB, source="gmail", board="JustJoin IT"), CFG)
    assert not fulltext.needs_text(replace(JOB, source="gmail", board="LinkedIn"), CFG)
    no_alerts = fulltext.Config(sources=("adzuna",))
    assert not fulltext.needs_text(replace(JOB, source="gmail", board="JustJoin IT"), no_alerts)


def test_alert_tracking_link_becomes_the_real_job_link():
    alert = replace(JOB, source="gmail", board="IrishJobs.ie",
                    url="https://click.mailtrack.example.com/ls/click?upn=abc&u=1")
    page = f"<main><p>{LONG}</p></main>"
    full = fulltext.read(alert, lambda url: ("https://www.irishjobs.ie/job/sre-1", page))
    assert full.url == "https://www.irishjobs.ie/job/sre-1"
    assert full.description_origin == "full page, www.irishjobs.ie"
    # Nothing better on the page: the real link is still kept.
    short = fulltext.read(alert, lambda url: ("https://www.irishjobs.ie/job/sre-1", "<p>x</p>"))
    assert short.url == "https://www.irishjobs.ie/job/sre-1"


def test_read_keeps_the_page_host_and_finds_years():
    page = f"<main><p>{LONG}</p></main>"
    full = fulltext.read(JOB, lambda url: ("https://careers.example.com/j/1", page))
    assert full.description_origin == "full page, careers.example.com"
    assert not full.description_is_snippet and full.years_required == 3
    assert description_blocks(full)[0] == (
        "Description source: adzuna (full page, careers.example.com)"
    )


def test_read_failure_or_shorter_text_keeps_the_snippet():
    def broken(url):
        raise http.HttpError("GET https://x.example.com/ failed: HTTP 403", 403)

    assert fulltext.read(JOB, broken) is None
    long_snippet = replace(JOB, description=LONG * 3)
    assert fulltext.read(long_snippet, lambda url: (url, f"<main>{LONG}</main>")) is None


def test_fill_respects_the_page_budget():
    groups = [[replace(JOB, dedupe_key=str(i), url=f"https://j.example.com/{i}")]
              for i in range(4)]
    read = []

    def get_page(url):
        read.append(url)
        return url, f"<main>{LONG}</main>"

    outcome = fulltext.fill(groups, fulltext.Config(max_pages=2), get_page)
    assert (outcome.tried, outcome.read) == (2, 2)
    assert [g[0].description_is_snippet for g in groups] == [False, False, True, True]


def test_sweep_reads_full_pages_only_for_jobs_it_may_keep():
    s = S.model_copy(update={"sweep": {**S.sweep, "daily_new_limit": 2,
                                       "fulltext": {"lookahead": 1}}})
    deps = fake_deps(s)
    real_page = deps.page
    read = []
    deps.page = lambda url: read.append(url) or real_page(url)
    summary = run_sweep(s, deps, TODAY)
    assert summary.new == 2
    assert 0 < len(read) <= 3  # never more than the daily room plus the lookahead


def test_fulltext_can_be_switched_off():
    s = S.model_copy(update={"sweep": {**S.sweep, "fulltext": {"enabled": False}}})
    summary = run_sweep(s, fake_deps(s), TODAY)
    assert not [n for n in summary.notes if n.startswith("Full descriptions")]


# ---------------------------------------------------------------- Workday and Amazon


BALTIC = TargetCompany("Baltic Bank", "https://balticbank.wd3.myworkdayjobs.com/careers",
                       "Workday", True)


def test_workday_postings_with_detail():
    http_fake = fakes.FixtureHttp(S)
    result = ats.fetch(S, [BALTIC], keep, get=http_fake, post=http_fake.post,
                       page=http_fake.page, today=TODAY)
    by_title = {p.title: p for p in result.postings}
    assert set(by_title) == {"DevOps Engineer", "Cloud Engineer"}  # Java Developer dropped
    devops = by_title["DevOps Engineer"]
    assert devops.location_text == "Warsaw, Poland"
    assert devops.posted_date == date(2026, 9, 29)  # startDate wins over "Posted 2 Days Ago"
    assert devops.posting_id == "workday-balticbank-DevOps-Engineer_JR1001"
    assert "3+ years of experience" in devops.description
    cloud = by_title["Cloud Engineer"]  # "2 Locations": the detail says where
    assert cloud.location_text == "Dublin, Ireland"
    assert cloud.posted_date == TODAY
    assert cloud.url == ("https://balticbank.wd3.myworkdayjobs.com/careers"
                         "/job/Dublin/Cloud-Engineer_JR1002")
    searches = [u for u in http_fake.requested if u.endswith("/careers/jobs")]
    assert len(searches) == len(ats.DEFAULT_SEARCH_TERMS)


def test_workday_without_detail_budget_drops_unknown_locations():
    s = S.model_copy(update={"sweep": {**S.sweep, "ats": {"workday_detail_calls": 0}}})
    http_fake = fakes.FixtureHttp(s)
    result = ats.fetch(s, [BALTIC], keep, get=http_fake, post=http_fake.post, today=TODAY,
                       page=http_fake.page)
    assert [(p.title, p.description, p.posted_date) for p in result.postings] == [
        ("DevOps Engineer", None, date(2026, 9, 29))
    ]


def test_workday_posted_text():
    assert ats.workday_posted("Posted Today", TODAY) == TODAY
    assert ats.workday_posted("Posted Yesterday", TODAY) == date(2026, 9, 30)
    assert ats.workday_posted("Posted 5 Days Ago", TODAY) == date(2026, 9, 26)
    assert ats.workday_posted("Posted 30+ Days Ago", TODAY) is None
    assert ats.workday_posted(None, TODAY) is None


def test_amazon_search_is_read_once_with_full_descriptions():
    http_fake = fakes.FixtureHttp(S)
    companies = [TargetCompany("Amazon", "https://www.amazon.jobs/en/", "Custom", True),
                 TargetCompany("AWS", "https://www.amazon.jobs/en/teams/aws", "Custom", True)]
    result = ats.fetch(S, companies, keep, get=http_fake, post=http_fake.post,
                       page=http_fake.page, today=TODAY)
    assert [(p.title, p.company, p.location_text) for p in result.postings] == [
        ("DevOps Engineer, Cloud Infrastructure", "Amazon", "Dublin, Ireland")
    ]
    job = result.postings[0]
    assert job.posted_date == date(2026, 9, 26)
    assert job.url == "https://www.amazon.jobs/en/jobs/2900001/devops-engineer-cloud-infrastructure"
    assert "Basic qualifications\n- 3+ years" in job.description
    assert "Preferred qualifications" in job.description
    searches = [u for u in http_fake.requested if "amazon.jobs" in u]
    assert len(searches) == len(ats.DEFAULT_SEARCH_TERMS)  # not twice for AWS


# ---------------------------------------------------------------- finding the board


def test_board_in_page_finds_the_linked_ats():
    page = ('<a href="https://boards.greenhouse.io/acme/jobs/1">Job</a>'
            '<script src="//boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
            '<a href="https://jobs.lever.co/other">Partner</a>')
    assert ats.board_in_page("https://acme.example.com/careers", page) == (
        ats.Board("greenhouse", "acme"))
    assert ats.board_in_page("https://acme.wd1.myworkdayjobs.com/External", "") == ats.Board(
        "workday", "acme", host="acme.wd1.myworkdayjobs.com", site="External")
    assert ats.board_in_page("https://acme.example.com/careers", "<p>Mail us</p>") is None


def test_careers_page_detection_is_cached_for_a_week():
    shamrock = TargetCompany("Shamrock Systems", "https://careers.shamrock.example.com/jobs",
                             "Custom", True)
    read = []

    def page(url):
        read.append(url)
        return url, '<iframe src="https://jobs.eu.lever.co/tulipdata"></iframe>'

    http_fake = fakes.FixtureHttp(S)
    state = FakeBotState()
    result = ats.fetch(S, [shamrock], keep, get=http_fake, post=http_fake.post, page=page,
                       state=state, today=TODAY)
    assert [p.title for p in result.postings] == ["Platform Engineer"]
    assert "ats: found the job board of 1 companies on their careers page or by their name" in (
        result.notes)
    assert state.get(ats.DETECT_KEY)["Shamrock Systems"] == {
        "ats": "lever", "token": "tulipdata", "eu": True, "host": "", "site": "",
        "checked": "2026-10-01", "url": "https://careers.shamrock.example.com/jobs"}

    ats.fetch(S, [shamrock], keep, get=http_fake, post=http_fake.post, page=page,
              state=state, today=date(2026, 10, 5))
    assert len(read) == 1  # cached
    ats.fetch(S, [shamrock], keep, get=http_fake, post=http_fake.post, page=page,
              state=state, today=date(2026, 10, 9))
    assert len(read) == 2  # a week later it is checked again


def test_blocked_careers_pages_are_listed_and_checked_weekly():
    companies = [TargetCompany(f"Co {i}", f"https://co{i}.example.com/careers", "Unknown", True)
                 for i in range(3)]
    s = S.model_copy(update={"sweep": {**S.sweep, "ats": {"max_detect_per_run": 2}}})
    read = []

    def page(url):
        read.append(url)
        raise http.HttpError(f"GET {url} failed: HTTP 403", 403)

    state = FakeBotState()
    result = ats.fetch(s, companies, keep, get=fakes.FixtureHttp(s), page=page, state=state,
                       today=TODAY)
    assert result.not_supported == ["Co 0", "Co 1", "Co 2"]
    assert result.blocked == ["Co 0 (HTTP 403)", "Co 1 (HTTP 403)"]  # Co 2: over the budget
    assert state.get(ats.DETECT_KEY)["Co 0"] == {"ats": None, "checked": "2026-10-01",
                                                 "problem": "HTTP 403",
                                                 "url": "https://co0.example.com/careers"}
    again = ats.fetch(s, companies, keep, get=fakes.FixtureHttp(s), page=page, state=state,
                      today=date(2026, 10, 3))
    assert again.blocked == ["Co 0 (HTTP 403)", "Co 1 (HTTP 403)", "Co 2 (HTTP 403)"]
    assert len(read) == 3  # Co 0 and Co 1 are not read again within the week


def test_blocked_site_found_by_its_name_on_a_public_ats():
    intel = TargetCompany("Tulip Data (NL)", "https://www.tulipdata.example.com/careers",
                          "Workday", True)

    def page(url):
        raise http.HttpError("GET https://www.tulipdata.example.com/careers failed: HTTP 403",
                             403)

    http_fake = fakes.FixtureHttp(S)
    result = ats.fetch(S, [intel], keep, get=http_fake, page=page, state=FakeBotState(),
                       today=TODAY)
    assert [p.title for p in result.postings] == ["Platform Engineer"]
    assert result.blocked == [] and result.not_supported == []
    assert "https://api.lever.co/v0/postings/tulipdata" in http_fake.requested


def test_name_slugs():
    assert ats.name_slugs("Intel (IE)") == ["intel"]
    assert ats.name_slugs("Odra Systems S.A.") == ["odrasystems", "odra-systems"]
    assert ats.name_slugs("Sii Sp. z o.o.") == ["sii"]
    assert ats.name_slugs("(IE)") == []


def test_friendly_summary_lists_sites_to_fix():
    from jobengine.sweep.models import SweepSummary
    summary = SweepSummary(blocked_sites=["Intel (IE) (HTTP 403)"],
                           no_board_sites=[f"Co {i}" for i in range(10)])
    text = summary.friendly_text()
    assert "- Blocked us: Intel (IE) (HTTP 403)" in text
    assert "- No job board found: Co 0, Co 1, Co 2, Co 3, Co 4, Co 5, Co 6, Co 7 and 2 more" in text


ADZ = replace(JOB, url="https://www.adzuna.pl/details/5901137322?utm_medium=api")


def test_adzuna_job_gets_the_employer_link_and_page():
    opened = []

    def get_page(url):
        opened.append(url)
        return "https://careers.example.com/jobs/77", f"<main>{LONG}</main>"

    full = fulltext.read(ADZ, get_page)
    assert opened == ["https://www.adzuna.pl/land/ad/5901137322"]
    assert full.url == "https://careers.example.com/jobs/77"
    assert full.description_origin == "full page, careers.example.com"


def test_adzuna_region_block_falls_back_to_the_details_page():
    def get_page(url):
        if "/land/ad/" in url:
            raise http.HttpError("GET https://www.adzuna.pl/land/ad/1 failed: HTTP 403", 403)
        return url, f"<main>{LONG}</main>"

    full = fulltext.read(ADZ, get_page)
    assert full.url == ADZ.url  # still the Adzuna link
    assert full.description_origin == "full page, www.adzuna.pl"


def test_employer_link_without_more_text_still_replaces_the_url():
    full = fulltext.read(ADZ, lambda url: ("https://careers.example.com/j", "<p>Apply</p>"))
    assert full.url == "https://careers.example.com/j"
    assert full.description == ADZ.description and full.description_is_snippet


def test_employer_link_only_for_adzuna_details():
    assert fulltext.employer_link("https://www.adzuna.nl/details/42?x=1") == (
        "https://www.adzuna.nl/land/ad/42")
    assert fulltext.employer_link("https://careers.example.com/details/42") is None


DELOITTE_URL = ("https://apply.deloittece.com/en_US/careers/SearchJobs/"
                "?523=%5B5515%5D&523_format=1482&listFilterMode=1")
LIST_PAGE = """<html><body><ul>
<li><h3><a href="/en_US/careers/JobDetail/DevOps-Engineer/52101">DevOps Engineer</a></h3>
    <div>Gdansk, Katowice, Warsaw - Poland</div><div>Experienced</div></li>
<li><h3><a href="/en_US/careers/JobDetail/Java-Developer/52102">Java Developer</a></h3>
    <div>Warsaw - Poland</div></li>
<li><h3><a href="/en_US/careers/JobDetail/Cloud-Engineer/52103">Cloud Engineer</a></h3>
    <div>Experienced</div></li>
<li><a href="/en_US/careers/JobDetail/DevOps-Engineer/52101">Apply now</a></li>
</ul></body></html>"""


def test_avature_search_page_is_detected():
    board = ats.detect_board(TargetCompany("Deloitte", DELOITTE_URL, "Custom", True), {})
    assert board == ats.Board("avature", DELOITTE_URL)


def test_avature_jobs_from_the_list_page():
    opened = []

    def page(url):
        opened.append(url)
        return url, LIST_PAGE

    deloitte = TargetCompany("Deloitte", DELOITTE_URL, "Custom", True, region="Poland")
    result = ats.fetch(S, [deloitte], keep, get=fakes.FixtureHttp(S), page=page, today=TODAY)
    assert [(p.title, p.location_text, p.posting_id) for p in result.postings] == [
        ("DevOps Engineer", "Gdansk, Katowice, Warsaw - Poland", "avature-52101"),
        ("Cloud Engineer", "Poland", "avature-52103"),  # no place shown: the row's Region
    ]
    assert result.postings[0].url == (
        "https://apply.deloittece.com/en_US/careers/JobDetail/DevOps-Engineer/52101")
    assert result.postings[0].description is None  # read later from the job page
    assert len(opened) == 1  # fewer than 50 jobs: one page
    assert "523=%5B5515%5D" in opened[0] and "jobRecordsPerPage=50" in opened[0]


def test_wrong_job_board_link_is_listed_to_fix():
    wrong = TargetCompany("Acme", "https://acme.wd1.myworkdayjobs.com/External", "Workday", True)

    def post(url, body):
        raise http.HttpError("POST https://acme.wd1.myworkdayjobs.com/x failed: HTTP 404", 404)

    result = ats.fetch(S, [wrong], keep, get=fakes.FixtureHttp(S), post=post,
                       page=fakes.FixtureHttp(S).page, today=TODAY)
    assert result.blocked == ["Acme (job board HTTP 404)"]
