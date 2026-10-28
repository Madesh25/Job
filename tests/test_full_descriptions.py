"""Full descriptions from company career sites (28 Oct, your request): Oracle jobs from the
site's own detail call, Phenom jobs from the page data, and nothing the bot had is lost.
Made-up jobs."""

import json
from dataclasses import replace

from test_fulltext import JOB, LONG

from jobengine import http
from jobengine.sweep import fulltext

ORACLE_URL = ("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/"
              "job/210612345")
ORACLE_JOB = replace(JOB, source="ats", board="Company site", company="JPMorgan Chase",
                     url=ORACLE_URL, description="Join our cloud platform team in Warsaw.",
                     description_is_snippet=False)
DETAIL = {"items": [{
    "ExternalDescriptionStr": f"<p>{LONG}</p>",
    "ExternalResponsibilitiesStr": "<ul><li>Run the Kubernetes platform</li></ul>",
    "ExternalQualificationsStr": "<ul><li>3+ years with Terraform</li></ul>",
}]}


def pages(answers):
    seen = []

    def get_page(url):
        seen.append(url)
        for part, answer in answers.items():
            if part in url:
                if isinstance(answer, Exception):
                    raise answer
                return url, answer
        raise http.HttpError("HTTP 404", 404)

    get_page.seen = seen
    return get_page


def test_the_oracle_detail_call():
    assert fulltext.oracle_detail_url(ORACLE_URL) == (
        "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;"
        "Id=%22210612345%22,siteNumber=CX_1001")
    assert fulltext.API_PATH in fulltext.oracle_detail_url(ORACLE_URL)
    assert fulltext.oracle_detail_url("https://boards.greenhouse.io/acme/jobs/1") is None


def test_an_oracle_job_gets_its_whole_text_and_keeps_its_summary():
    get_page = pages({"recruitingCEJobRequisitionDetails": json.dumps(DETAIL)})
    full = fulltext.read(ORACLE_JOB, get_page)
    assert full.description.startswith("Join our cloud platform team in Warsaw.\n\n")
    assert "Responsibilities\nRun the Kubernetes platform" in full.description
    assert "Qualifications\n3+ years with Terraform" in full.description
    assert full.description_origin == "job details, jpmc.fa.oraclecloud.com"
    assert full.years_required == 3
    assert len(get_page.seen) == 1  # the JavaScript page itself is not opened


def test_a_failed_detail_call_falls_back_to_the_page():
    get_page = pages({"recruitingCEJobRequisitionDetails": http.HttpError("HTTP 500", 500),
                      "/job/210612345": "<html><body>Loading...</body></html>"})
    assert fulltext.read(ORACLE_JOB, get_page) is None  # nothing better: the summary stays


PHENOM_PAGE = ("<html><head><script>phApp.ddo = {\"jobDetail\":{\"status\":200,\"data\":{"
               "\"job\":{\"title\":\"DevOps Engineer\",\"descriptionTeaser\":\""
               + LONG[:120] + "\",\"description\":\"<p>" + LONG + "</p><p>Hybrid in "
               "Krakow.</p>\"}}}};</script></head><body><div id=\"app\"></div></body></html>")


def test_a_phenom_job_page_gives_its_text_from_the_page_data():
    text = fulltext.extract(PHENOM_PAGE)
    assert text is not None and "Hybrid in Krakow." in text
    assert text.startswith(LONG[:60]) and text.count(LONG) == 1
    assert LONG[:120] + "\n" not in text  # the teaser is not added again


def test_a_company_summary_is_kept_an_adzuna_snippet_is_not():
    page = f"<html><body><main>{LONG}</main></body></html>"
    company = replace(ORACLE_JOB, url="https://careers.example.com/job/1")
    full = fulltext.read(company, pages({"careers.example.com": page}))
    assert full.description.startswith("Join our cloud platform team in Warsaw.\n\n")
    adzuna = fulltext.read(JOB, pages({"adzuna": page}))
    assert not adzuna.description.startswith("We run services.")


def test_keep_old_adds_nothing_twice():
    assert fulltext.keep_old("Short intro.", "Short intro. And the rest.") == \
        "Short intro. And the rest."
    assert fulltext.keep_old(None, "New text") == "New text"
    assert fulltext.keep_old("Summary", "Other text") == "Summary\n\nOther text"
