"""FETCH 13 (5 Oct): company career sites that really work. Ashby boards, a Workday search
word the site refuses, a changed Careers URL, the 20-day limit for company sites, and the
--check-boards command."""

from datetime import date

import pytest

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.settings import load_settings
from jobengine.sweep import boards
from jobengine.sweep.models import TargetCompany
from jobengine.sweep.runner import age_limit, ats_max_posted_age, max_posted_age
from jobengine.sweep.sources import ats

S = load_settings("local", {})
TODAY = date(2026, 10, 5)
MOLLIE = TargetCompany("Mollie", "https://jobs.ashbyhq.com/mollie", "Unknown", True,
                       "Netherlands")


def keep_all(title, location):
    return True


ASHBY_ANSWER = {"jobs": [
    {"id": "a1", "title": "Platform Engineer II", "location": "Amsterdam",
     "secondaryLocations": [{"location": "Utrecht"}], "isListed": True, "isRemote": False,
     "publishedAt": "2026-10-02T09:00:00.000+00:00",
     "jobUrl": "https://jobs.ashbyhq.com/mollie/a1",
     "address": {"postalAddress": {"addressCountry": "Netherlands"}},
     "descriptionPlain": "Run Kubernetes on GCP.",
     "compensation": {"compensationTierSummary": "EUR 70K - 90K"}},
    {"id": "a2", "title": "Hidden role", "location": "Amsterdam", "isListed": False},
]}


def test_ashby_links_and_board_are_read():
    board = ats.board_from_url("https://jobs.ashbyhq.com/mollie/dea4f8ea-e3a7")
    assert board == ats.Board("ashby", "mollie")
    asked = []

    def get(url, params):
        asked.append((url, params))
        return ASHBY_ANSWER

    postings, dropped, _ = ats.ashby(MOLLIE, board, get, keep_all)
    assert asked[0][0] == "https://api.ashbyhq.com/posting-api/job-board/mollie"
    assert [p.title for p in postings] == ["Platform Engineer II"]  # unlisted one left out
    p = postings[0]
    assert p.location_text == "Amsterdam / Utrecht, Netherlands"
    assert p.posted_date == date(2026, 10, 2) and p.url.endswith("/mollie/a1")
    assert p.salary_text == "EUR 70K - 90K (Ashby)"
    assert p.description == "Run Kubernetes on GCP." and p.posting_id == "ashby-mollie-a1"
    assert dropped == 0


def test_ashby_host_is_allowed():
    assert "api.ashbyhq.com" in S.allowed_hosts


WD = ats.Board("workday", "dell", host="dell.wd1.myworkdayjobs.com", site="External")
DELL = TargetCompany("Dell Technologies", "https://dell.wd1.myworkdayjobs.com/External",
                     "Workday", True)


def test_a_refused_workday_search_word_keeps_the_other_words_jobs():
    def post(url, body):
        if body["searchText"] == "sre":
            raise http.HttpError("POST failed: HTTP 422", 422)
        return {"total": 1, "jobPostings": [
            {"title": "DevOps Engineer", "externalPath": "/job/Krakow/DevOps_R1",
             "locationsText": "Krakow, Poland", "postedOn": "Posted Today"}]}

    postings, _, _ = ats.workday(DELL, WD, lambda u, p: {}, post, keep_all, 0, TODAY,
                                 ("devops", "sre"), 1)
    assert [p.title for p in postings] == ["DevOps Engineer"]

    def refused(url, body):
        raise http.HttpError("POST failed: HTTP 422", 422)

    with pytest.raises(http.HttpError):
        ats.workday(DELL, WD, lambda u, p: {}, refused, keep_all, 0, TODAY, ("devops", "sre"), 1)


def test_a_changed_careers_url_is_checked_again_at_once():
    state = FakeBotState()
    # Saved on 28 Sep for the old link (and before links were noted): "no board".
    state.set(ats.DETECT_KEY, {"Mollie": {"ats": None, "checked": "2026-10-04"}})
    old = TargetCompany("Mollie", "https://www.mollie.com/careers", "Unknown", True)
    detector = ats.Detector(S, None, None, state, TODAY)
    decided, board = detector._cached(old)
    assert not decided  # no link noted: checked again
    detector._record(old, None, None)
    assert detector._cached(old) == (True, None)  # same link, within the week: cached
    new = TargetCompany("Mollie", "https://jobs.ashbyhq.com/mollie", "Unknown", True)
    assert detector._cached(new) == (False, None)  # the link changed: checked again


def test_company_sites_keep_jobs_for_20_days_other_sources_3():
    assert ats_max_posted_age(S) == 20
    assert max_posted_age.__name__  # the general limit stays sweep.max_posted_age_days
    assert age_limit("ats", 2, 20) == 20
    assert age_limit("adzuna", 2, 20) == 2
    assert age_limit("ats", None, 20) is None  # no limit stays no limit


# ---------------------------------------------------------------- --check-boards


def test_platforms_are_named_from_their_links():
    assert boards.platform_of("https://careers.wipro.com/job/DevOps-Engineer/90083-en_US/") == \
        "SuccessFactors"
    assert boards.platform_of("https://jobs.sap.com/job/Warszawa-Senior-DevOps/1282385101/") == \
        "SuccessFactors"
    assert boards.platform_of(
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs") == \
        "Oracle Cloud HCM"
    assert boards.platform_of("https://jobs.ericsson.com/careers?query=x&domain=ericsson.com") == \
        "Eightfold"
    assert boards.platform_of("https://careers.cisco.com/global/en/search-results?k=1") == \
        "Phenom"
    page = '<a href="/global/en/job/1/A">' * 3
    assert boards.platform_of("https://careers.example.com/", page) == "Phenom"
    assert boards.platform_of("https://careers.example.com/", "<html>nothing</html>") == ""


def test_candidates_file_is_read(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("proposed:\n  Mollie: https://jobs.ashbyhq.com/mollie\n  Empty:\n")
    assert boards.load_candidates(path) == {"Mollie": "https://jobs.ashbyhq.com/mollie"}
    assert boards.load_candidates(tmp_path / "missing.yaml") == {}
    real = boards.load_candidates()
    assert real == {}  # every proposed link has been checked and moved to Notion (5 Oct)
    # Every proposed link is one /fetch can read (a reader exists for it).
    assert [n for n, link in real.items() if ats.board_from_url(link) is None] == []


def test_check_reads_boards_counts_jobs_and_saves_other_pages(tmp_path):
    companies = [
        MOLLIE,
        TargetCompany("Mollie Copy", "https://jobs.ashbyhq.com/mollie", "Unknown", True),
        TargetCompany("Wipro", "https://careers.wipro.com", "Unknown", True, "Poland"),
        TargetCompany("Gone", "https://gone.example.com/careers", "Unknown", True),
        TargetCompany("Off", "https://off.example.com", "Unknown", False),
    ]
    candidates = {"Wipro": "https://careers.wipro.com/search/?q=devops",
                  "Not A Company": "https://x.example.com"}
    gets = []

    def get(url, params):
        gets.append(url)
        return ASHBY_ANSWER

    def page(url):
        if "gone" in url:
            raise http.HttpError("GET failed: HTTP 404", 404)
        return url, '<a href="/job/Warszawa-DevOps-Engineer/1282385101/">x</a>' * 3

    rows = boards.run(S, companies, candidates, get, lambda u, b: {}, page, TODAY, tmp_path)
    by = {r.company: r for r in rows}
    assert (by["Mollie"].result, by["Mollie"].jobs, by["Mollie"].here) == ("OK", 1, 1)
    assert len(gets) == 1  # the copy with the same link is not read again
    assert by["Mollie Copy"].jobs == 1
    wipro = by["Wipro"]
    assert (wipro.origin, wipro.result, wipro.platform) == ("proposed", "no reader yet",
                                                            "SuccessFactors")
    assert (tmp_path / "wipro.html").exists()
    assert by["Gone"].result == "HTTP 404"
    assert "Off" not in by
    assert by["Not A Company"].result == "not in Target Companies (check the name)"
    text = boards.report_text(rows)
    assert text.startswith("Job board check: 2 of 5 companies readable by /fetch")
    assert "- Mollie [Notion]: OK | ashby | 1 jobs, 1 in PL/NL/IE" in text
    csv_path = boards.save_csv(rows, tmp_path)
    assert csv_path.read_text().splitlines()[0].startswith("Company,Region,Checked link")


def test_a_board_linked_from_a_careers_page_is_read(tmp_path):
    company = TargetCompany("Acme", "https://acme.example.com/careers", "Unknown", True)

    def page(url):
        return url, '<iframe src="https://jobs.ashbyhq.com/acme"></iframe>'

    rows = boards.run(S, [company], {}, lambda u, p: ASHBY_ANSWER, lambda u, b: {}, page,
                      TODAY, tmp_path)
    assert (rows[0].result, rows[0].platform, rows[0].found_link) == ("OK", "ashby", "acme")


# ---------------------------------------------------------------- FETCH 13b readers


def test_new_board_links_are_recognised():
    sf = ats.board_from_url("https://careers.capgemini.com/services/rss/job/"
                            "?locale=en_GB&keywords=(devops)")
    assert sf == ats.Board("successfactors", "https://careers.capgemini.com", site="en_GB")
    ph = ats.board_from_url("https://careers.allianz.com/global/en/search-results?keywords=x")
    assert ph == ats.Board("phenom", "https://careers.allianz.com/global/en")
    oc = ats.board_from_url("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/"
                            "sites/CX_1001/jobs?keyword=devops")
    assert oc == ats.Board("oracle", "CX_1001", host="jpmc.fa.oraclecloud.com")
    assert "oraclecloud.com" in S.allowed_host_suffixes


def test_workday_searches_only_poland_netherlands_ireland_when_the_site_allows():
    bodies = []
    facets = [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationCountry", "values": [
            {"descriptor": "Poland", "id": "pl1", "count": 4},
            {"descriptor": "India", "id": "in1", "count": 90},
            {"descriptor": "Ireland", "id": "ie1", "count": 2}]}]}]

    def post(url, body):
        bodies.append(body)
        return {"total": 0, "jobPostings": [], "facets": facets}

    ats.workday(DELL, WD, lambda u, p: {}, post, keep_all, 0, TODAY, ("devops", "sre"), 1)
    assert bodies[0]["appliedFacets"] == {}  # the first answer names the filter
    assert [b["appliedFacets"] for b in bodies[1:]] == [{"locationCountry": ["pl1", "ie1"]}] * 2


FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Jobs</title>
<item><title>DevOps Engineer Medior (Utrecht, NL, 3542 AB)</title>
<link>https://careers.capgemini.com/job/Utrecht-DevOps-Engineer-Medior/1378157633/</link>
<description>&lt;p&gt;Run &lt;b&gt;Kubernetes&lt;/b&gt; pipelines.&lt;/p&gt;</description>
<pubDate>Sat, 04 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Data Engineer (Aguascalientes, MX)</title>
<link>https://careers.capgemini.com/job/Aguascalientes-Data-Engineer/1439116633/</link>
</item></channel></rss>"""


def test_successfactors_job_feed_is_read():
    asked = []

    def page(url):
        asked.append(url)
        return url, FEED

    board = ats.Board("successfactors", "https://careers.capgemini.com", site="en_US")
    postings, _, _ = ats.successfactors(MOLLIE, board, page, keep_all, ("devops", "sre"))
    assert asked[0].startswith("https://careers.capgemini.com/services/rss/job/?locale=en_US"
                               "&keywords=%28devops%29")
    assert len(postings) == 2  # the same jobs in the second search count once
    p = postings[0]
    assert (p.title, p.location_text) == ("DevOps Engineer Medior", "Utrecht, Netherlands")
    assert p.posted_date == date(2026, 10, 4) and p.posting_id == "sf-1378157633"
    assert "Kubernetes" in p.description and "<b>" not in p.description
    assert ats.feed_items("<html>not a feed</html>") == []


PHENOM_PAGE = ('<script>phApp.ddo = {"eagerLoadRefineSearch":{"status":200,"hits":1,'
               '"totalHits":1,"data":{"jobs":[{"title":"DevOps Engineer (m/f/d)",'
               '"jobId":"106048","jobSeqNo":"AISAIPGB106048EXTERNALENGLOBAL",'
               '"cityStateCountry":"WARSZAWA, Mazowieckie, Poland","country":"Poland",'
               '"postedDate":"2026-09-11T06:58:16.000+0000",'
               '"descriptionTeaser":"Build CI/CD on Azure."}]}}};</script>')


def test_phenom_page_data_is_read():
    board = ats.Board("phenom", "https://careers.allianz.com/global/en")
    asked = []

    def page(url):
        asked.append(url)
        return url, PHENOM_PAGE

    postings, _, _ = ats.phenom(MOLLIE, board, page, keep_all, ("devops",))
    assert asked == ["https://careers.allianz.com/global/en/search-results?keywords=devops&from=0"]
    p = postings[0]
    assert p.location_text == "WARSZAWA, Mazowieckie, Poland"
    assert p.url == "https://careers.allianz.com/global/en/job/106048/DevOps-Engineer-m-f-d"
    assert p.posted_date == date(2026, 9, 11) and p.description == "Build CI/CD on Azure."
    assert ats.phenom_jobs("<html></html>") == []


ORACLE_ANSWER = {"items": [{"TotalJobsCount": 1, "requisitionList": [
    {"Id": "210744868", "Title": "Site Reliability Engineer III", "PostedDate": "2026-09-30",
     "PrimaryLocation": "Warsaw, Poland",
     "secondaryLocations": [{"Name": "Glasgow, United Kingdom"}],
     "ShortDescriptionStr": "Keep trading systems up."}]}]}


def test_oracle_candidate_site_search_is_read():
    board = ats.Board("oracle", "CX_1001", host="jpmc.fa.oraclecloud.com")
    asked = []

    def get(url, params):
        asked.append((url, params))
        return ORACLE_ANSWER

    postings, _, _ = ats.oracle(MOLLIE, board, get, keep_all, ("devops", "sre"))
    url, params = asked[0]
    assert url == ("https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/"
                   "recruitingCEJobRequisitions")
    assert 'siteNumber=CX_1001' in params["finder"] and 'keyword="devops"' in params["finder"]
    assert len(postings) == 1
    p = postings[0]
    assert p.location_text == "Warsaw, Poland / Glasgow, United Kingdom"
    assert p.url.endswith("/sites/CX_1001/job/210744868")
    assert p.posted_date == date(2026, 9, 30) and p.posting_id == "oracle-jpmc-210744868"


# ---------------------------------------------------------------- FETCH 13c fixes


def test_a_job_feed_request_accepts_rss():
    import httpx

    seen = []

    def answer(request):
        seen.append(request.headers["accept"])
        if "rss+xml" not in request.headers["accept"]:
            return httpx.Response(406)
        return httpx.Response(200, headers={"content-type": "application/rss+xml"},
                              content=FEED.encode())

    http.set_transport(httpx.MockTransport(answer))
    try:
        _, text = http.get_page("https://careers.capgemini.com/services/rss/job/?q=x", s=S,
                                accept_feed=True)
        with pytest.raises(http.HttpError):  # a web page request is refused, as on 5 Oct
            http.get_page("https://careers.capgemini.com/services/rss/job/?q=x", s=S)
    finally:
        http.set_transport(None)
    assert "<item>" in text and "application/rss+xml" in seen[0]


def test_workday_without_a_country_filter_uses_hub_city_locations():
    bodies = []
    facets = [{"facetParameter": "locations", "values": [
        {"descriptor": "Warsaw, Poland", "id": "w1"}, {"descriptor": "Bengaluru", "id": "b1"},
        {"descriptor": "Dublin", "id": "d1"}, {"descriptor": "Amsterdam Zuid", "id": "a1"}]}]

    def post(url, body):
        bodies.append(body)
        return {"total": 0, "jobPostings": [], "facets": facets}

    ats.workday(DELL, WD, lambda u, p: {}, post, keep_all, 0, TODAY, ("devops",), 1)
    assert bodies[1]["appliedFacets"] == {"locations": ["w1", "d1", "a1"]}


def test_oracle_searches_each_of_our_countries_when_the_site_names_them():
    finders = []
    first = {"items": [{"requisitionList": [], "locationsFacet": [
        {"Id": 111, "Name": "Poland"}, {"Id": 222, "Name": "India"},
        {"Id": 333, "Name": "Ireland"}]}]}

    def get(url, params):
        finders.append(params["finder"])
        return first if len(finders) == 1 else ORACLE_ANSWER

    board = ats.Board("oracle", "CX_1001", host="jpmc.fa.oraclecloud.com")
    postings, _, _ = ats.oracle(MOLLIE, board, get, keep_all, ("devops", "sre"))
    assert "selectedLocationsFacet" not in finders[0]
    assert [f.split("selectedLocationsFacet=")[1] for f in finders[1:]] == \
        ["111", "333", "111", "333"]
    assert len(postings) == 1  # the same job found in each search counts once


def test_check_keeps_the_first_answer_when_no_job_is_local(tmp_path):
    company = TargetCompany("Far Away", "https://jobs.ashbyhq.com/far", "Unknown", True)
    answer = {"jobs": [{"id": "x", "title": "DevOps Engineer", "location": "Bengaluru"}]}
    rows = boards.run(S, [company], {}, lambda u, p: answer, lambda u, b: {},
                      lambda u: (u, ""), TODAY, tmp_path)
    assert (rows[0].jobs, rows[0].here) == (1, 0)
    assert (tmp_path / "far-away-answer.json").read_text().startswith("{")


# ---------------------------------------------------------------- FETCH 13d


def _pracuj_card(job_id, lines_before, title, lines_after):
    link = f"https://pracuj.pl/praca/x,oferta,{job_id}?sendid=1&utm_source=rekomendacje"
    before = "".join(f"<div>{line}</div>" for line in lines_before)
    after = "".join(f"<div>{line}</div>" for line in lines_after)
    return (f'<table><tr><td><a href="{link}"><img src="logo.png"></a></td><td>'
            f'<div><a href="{link}">{before}<span>{title}</span></a></div>{after}'
            f'<a href="{link}">{lines_after[-2] if len(lines_after) > 1 else ""}</a>'
            f"</td></tr></table>")


PRACUJ = "<html><body>" + "".join([
    _pracuj_card("1005093474", ["!"], "DevOps Engineer (Mid/Senior)",
                 ["Najlepiej dopasowana", "20400-28800 zł netto (+ VAT) / mies.",
                  "CodeTalent Sp. z o.o.", "Warszawa"]),
    _pracuj_card("1005075978", ["!"], "DevOps Engineer (LLM Platform)",
                 ["Śpiesz się!", "NATEK POLAND", "Warszawa"]),
    _pracuj_card("1005111810", [], "Administrator IT K/M",
                 ["Nowość!", "8000-10000 zł brutto / mies.", "Comp S.A.", "Zabrze"]),
]) + "</body></html>"


def test_pracuj_alert_cards_skip_badges_and_keep_the_salary():
    from jobengine.gmail_reader import GmailMessage
    from jobengine.sweep.sources import gmail_alerts

    message = GmailMessage(id="p1", sender='"Pracuj.pl" <rekomendacje@wysylka.pracuj.pl>',
                           subject="DevOps Engineer (Mid/Senior) - oferta", html=PRACUJ)
    postings = gmail_alerts.parse_alert(message, S.sweep["gmail"])
    got = [(p.board, p.title, p.company, p.location_text, p.posting_id) for p in postings]
    assert got == [
        ("Pracuj.pl", "DevOps Engineer (Mid/Senior)", "CodeTalent Sp. z o.o.", "Warszawa",
         "1005093474"),
        ("Pracuj.pl", "DevOps Engineer (LLM Platform)", "NATEK POLAND", "Warszawa",
         "1005075978"),
        ("Pracuj.pl", "Administrator IT K/M", "Comp S.A.", "Zabrze", "1005111810"),
    ]
    assert postings[0].salary_text == "20400-28800 zł netto (+ VAT) / mies. (Pracuj.pl)"
    assert postings[1].salary_text is None


def test_a_workday_job_without_a_place_is_placed_by_its_detail():
    def post(url, body):
        return {"total": 1, "jobPostings": [
            {"title": "DevOps Engineer", "externalPath": "/job/x/DevOps_R9"}]}

    def get(url, params):
        return {"jobPostingInfo": {"title": "DevOps Engineer", "location": "Warsaw",
                                   "country": {"descriptor": "Poland"}}}

    def in_poland(title, location):
        return "Poland" in location

    postings, dropped, _ = ats.workday(DELL, WD, get, post, in_poland, 5, TODAY, ("devops",), 1)
    assert [p.location_text for p in postings] == ["Warsaw, Poland"] and dropped == 0
    postings, dropped, _ = ats.workday(DELL, WD, get, post, in_poland, 0, TODAY, ("devops",), 1)
    assert postings == [] and dropped == 1  # no detail call left: where it is stays unknown


def test_dell_oracle_site_on_its_own_domain():
    board = ats.board_from_url("https://enterpriseplatform.dell.com/hcmUI/CandidateExperience/"
                               "en/sites/careers/job/295767/?utm_medium=jobshare")
    assert board == ats.Board("oracle", "careers", host="enterpriseplatform.dell.com")
    assert "enterpriseplatform.dell.com" in S.allowed_hosts


def test_oracle_places_named_with_their_country_are_searched():
    finders = []
    first = {"items": [{"requisitionList": [], "locationsFacet": [
        {"Id": 1, "Name": "Warsaw, Poland"}, {"Id": 2, "Name": "Pune, India"},
        {"Id": 3, "Name": "Dublin, Ireland"}]}]}

    def get(url, params):
        finders.append(params["finder"])
        return first if len(finders) == 1 else {}

    ats.oracle(MOLLIE, ats.Board("oracle", "careers", host="enterpriseplatform.dell.com"), get,
               keep_all, ("devops",))
    assert [f.split("selectedLocationsFacet=")[1] for f in finders[1:]] == ["1", "3"]


# ---------------------------------------------------------------- own sites, Oracle lookup


def test_own_site_companies_are_not_looked_up_and_only_counted():
    from jobengine.sweep.models import SweepSummary

    cfg = {**S.sweep, "ats": {**(S.sweep.get("ats") or {}),
                              "own_site_companies": ["Infosys", "Google"]}}
    s = S.model_copy(update={"sweep": cfg})
    companies = [
        TargetCompany("Infosys (IE)", "https://www.infosys.com/careers", "Custom", True),
        TargetCompany("Google", "https://www.google.com/about/careers/", "Custom", True),
        # On the list, but its Careers URL is a readable board: read anyway.
        TargetCompany("Infosys", "https://jobs.ashbyhq.com/infosys", "Custom", True),
    ]
    pages = []

    def page(url):
        pages.append(url)
        return url, "<html></html>"

    result = ats.fetch(s, companies, keep_all, get=lambda u, p=None: ASHBY_ANSWER,
                       post=lambda u, b: {}, page=page, state=FakeBotState(), today=TODAY)
    assert result.own_site == ["Infosys (IE)", "Google"]
    assert pages == []  # their careers pages are not read
    assert result.no_board == [] and result.not_supported == []
    assert [p.company for p in result.postings] == ["Infosys"]
    summary = SweepSummary()
    summary.own_site_count = 2
    assert "Own job sites, covered by your alerts (not read here): 2 companies" in \
        summary.friendly_text()


def test_real_config_names_the_own_site_companies():
    names = {ats.canon_name(n) for n in S.sweep["ats"]["own_site_companies"]}
    assert {"google", "infosys", "tata consultancy services", "microsoft"} <= names
    assert ats.canon_name("Tech Mahindra (NL)") in names


def test_oracle_finds_a_country_missing_from_the_biggest_places():
    finders = []
    us_only = {"items": [{"requisitionList": [], "locationsFacet": [
        {"Id": 1, "Name": "United States"}, {"Id": 2, "Name": "NY, United States"}]}]}

    def get(url, params):
        finders.append(params["finder"])
        if 'keyword="Poland"' in params["finder"]:
            return {"items": [{"locationsFacet": [{"Id": 77, "Name": "Poland"},
                                                  {"Id": 78, "Name": "Warsaw, Poland"}]}]}
        if 'keyword="Netherlands"' in params["finder"] or 'keyword="Ireland"' in params["finder"]:
            return {"items": [{"locationsFacet": [{"Id": 1, "Name": "United States"}]}]}
        return us_only if len(finders) == 1 else {}

    ats.oracle(MOLLIE, ats.Board("oracle", "CX_1001", host="jpmc.fa.oraclecloud.com"), get,
               keep_all, ("devops",))
    searched = [f for f in finders if "selectedLocationsFacet" in f]
    assert len(searched) == 1 and searched[0].endswith("selectedLocationsFacet=77")
    assert 'keyword="devops"' in searched[0]


def test_check_names_own_site_companies_without_reading_them(tmp_path):
    company = TargetCompany("Google (IE)", "https://www.google.com/about/careers/", "Custom",
                            True)

    def page(url):
        raise AssertionError("an own job site is not read by the check")

    rows = boards.run(S, [company], {}, lambda u, p: {}, lambda u, b: {}, page, TODAY,
                      tmp_path)
    assert rows[0].result == "own job site, covered by your alerts"
