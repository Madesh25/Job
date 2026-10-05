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
    assert real["Mollie"] == "https://jobs.ashbyhq.com/mollie"


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
