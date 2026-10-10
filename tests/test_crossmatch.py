"""LinkedIn descriptions from the same job on another site (sweep/crossmatch.py)."""

from dataclasses import replace
from datetime import date

import pytest

from jobengine import http
from jobengine.notion_repo import FakeJobsRepo
from jobengine.settings import load_settings
from jobengine.sweep import crossmatch, fakes, fulltext
from jobengine.sweep.models import Job, RawPosting, SourceResult
from jobengine.sweep.runner import SweepDeps, run_sweep

S = load_settings("local", {})
TODAY = date(2026, 10, 1)
CFG = fulltext.Config()
LONG = " ".join(["Operate Kubernetes and Terraform on AWS with 3 years of experience."] * 12)

ADZUNA = Job(
    source="adzuna", board="Adzuna", company="Vistula Cloud Sp. z o.o.",
    role="DevOps Engineer (Remote)", city="Krakow", country="Poland",
    url="https://www.adzuna.pl/details/1", posted_date=None, salary=None, seniority="Unknown",
    years_required=None, dedupe_key="k1", posting_ref="adzuna:1", description="Short snippet.",
    description_is_snippet=True,
)
ATS = replace(ADZUNA, source="ats", board="Company site", role="DevOps Engineer",
              url="https://boards.greenhouse.io/vistula/jobs/2", posting_ref="ats:2",
              description=LONG, description_is_snippet=False)


def linkedin_row(**extra):
    row = {"page_id": "li-1", "Company": "Vistula Cloud", "Role": "DevOps Engineer",
           "Country": "Poland", "City": "Warszawa", "Board": "LinkedIn",
           "Screen verdict": "Unscreened", "Status": "New"}
    row.update(extra)
    return row


def page(url):
    return url, f"<html><body><main><h1>DevOps Engineer</h1><p>{LONG}</p></main></body></html>"


def test_title_key_ignores_work_mode_and_place_only():
    assert crossmatch.title_key("DevOps Engineer (Remote)") == "devops engineer"
    assert crossmatch.title_key("DevOps Engineer - Hybrid, Warsaw", "Warsaw") == "devops engineer"
    assert crossmatch.title_key("DevOps Engineer full-time", country="Poland") == \
        "devops engineer"
    assert crossmatch.title_key("Senior DevOps Engineer") == "senior devops engineer"
    assert crossmatch.match_key("Vistula Cloud Sp. z o.o.", "DevOps Engineer", "Poland",
                                "Krakow") == \
        crossmatch.match_key("Vistula Cloud", "DevOps Engineer", "Poland", "Warszawa")
    assert crossmatch.match_key("Vistula Cloud", "DevOps Engineer", "Poland") != \
        crossmatch.match_key("Vistula Cloud", "DevOps Engineer", "Netherlands")


def test_pool_prefers_a_full_description_and_ignores_linkedin():
    linkedin = replace(ATS, board="LinkedIn", source="gmail", description=LONG * 2)
    pool = crossmatch.Pool([ADZUNA, linkedin, ATS])
    found = pool.find("Vistula Cloud", "DevOps Engineer", "Poland", "Warszawa")
    assert found == [ATS, ADZUNA]
    assert pool.find("Vistula Cloud", "Senior DevOps Engineer", "Poland") == []
    assert not crossmatch.Pool([linkedin])


def test_fills_a_linkedin_row_from_the_company_board():
    repo = FakeJobsRepo([linkedin_row()])
    filled = crossmatch.fill_linkedin(repo, [ADZUNA, ATS], CFG, None, set())
    assert filled.labels == ["Vistula Cloud, DevOps Engineer (from Company site)"]
    body = repo.read_body("li-1")
    assert body[0] == ("Description source: ats (same job on Company site, found for this "
                       "LinkedIn alert: https://boards.greenhouse.io/vistula/jobs/2)")
    assert "".join(body[1:]) == LONG


def test_reads_the_page_when_the_match_is_only_a_snippet():
    repo = FakeJobsRepo([linkedin_row()])
    filled = crossmatch.fill_linkedin(repo, [ADZUNA], CFG, page, set())
    assert filled.labels == ["Vistula Cloud, DevOps Engineer (from Adzuna)"]
    assert "Operate Kubernetes" in "".join(repo.read_body("li-1"))
    # Without a page reader (or budget) a snippet is never used.
    repo = FakeJobsRepo([linkedin_row()])
    filled = crossmatch.fill_linkedin(repo, [ADZUNA], CFG, None, set())
    assert filled.labels == [] and filled.snippets_only == 1
    assert repo.read_body("li-1") == []
    filled = crossmatch.fill_linkedin(repo, [ADZUNA], CFG, page, set(), max_pages=0)
    assert filled.labels == []


def test_rows_left_alone():
    repo = FakeJobsRepo([
        linkedin_row(page_id="has-body", body=["Description source: pasted (full)", LONG]),
        linkedin_row(page_id="screened", **{"Screen verdict": "Apply high"}),
        linkedin_row(page_id="other-title", Role="Platform Engineer"),
        linkedin_row(page_id="other-board", Board="Adzuna"),
    ])
    filled = crossmatch.fill_linkedin(repo, [ATS], CFG, None, set())
    assert filled.labels == []
    assert [w for w in repo.writes if w[0] == "append_body"] == []


def test_sweep_fills_an_old_linkedin_row_from_a_posting_in_another_city():
    repo = FakeJobsRepo([linkedin_row(**{
        "Dedupe key": "vistula cloud|devops engineer|warszawa", "Posting IDs": "gmail:li-1",
        "Times seen": 1, "First seen": date(2026, 9, 30)})])
    posting = RawPosting(
        source="ats", board="Company site", title="DevOps Engineer", company="Vistula Cloud",
        location_text="Krakow, Poland", url="https://boards.greenhouse.io/vistula/jobs/2",
        posting_id="2", description=LONG,
    )
    deps = SweepDeps(
        config=fakes.config, companies=lambda: [], repo=repo,
        gmail=lambda: SourceResult("gmail"),
        adzuna=lambda: SourceResult("adzuna"),
        ats=lambda companies, keep, state=None: SourceResult("ats", postings=[posting]),
    )
    summary = run_sweep(S, deps, TODAY)
    assert summary.other_cities == 1  # the Krakow posting is not saved as a second job
    assert summary.linkedin_filled == ["Vistula Cloud, DevOps Engineer (from Company site)"]
    assert "Operate Kubernetes" in "".join(repo.read_body("li-1"))
    text = summary.friendly_text()
    assert ("\U0001F517 Descriptions taken from the same job on another site (alerts, Adzuna "
            "snippets; no /jd needed): 1" in text)
    assert "- Vistula Cloud, DevOps Engineer (from Company site)" in text


def test_cross_match_can_be_switched_off():
    repo = FakeJobsRepo([linkedin_row()])
    s = S.model_copy(update={"sweep": {**S.sweep, "crossmatch": {"enabled": False}}})
    posting = RawPosting(
        source="ats", board="Company site", title="DevOps Engineer", company="Vistula Cloud",
        location_text="Krakow, Poland", url="https://boards.greenhouse.io/vistula/jobs/2",
        posting_id="2", description=LONG,
    )
    deps = SweepDeps(
        config=fakes.config, companies=lambda: [], repo=repo,
        gmail=lambda: SourceResult("gmail"), adzuna=lambda: SourceResult("adzuna"),
        ats=lambda companies, keep, state=None: SourceResult("ats", postings=[posting]),
    )
    assert run_sweep(s, deps, TODAY).linkedin_filled == []
    assert repo.read_body("li-1") == []


NOFLUFF = replace(ATS, source="nofluffjobs", board="NoFluffJobs",
                  url="https://nofluffjobs.com/pl/job/devops-engineer-vistula-cloud-warszawa")


def test_blocked_boards_are_filled_too():
    """10 Oct: JustJoin IT and theprotocol.it are never read (their terms forbid automatic
    downloading); their alert jobs take the description of the same job on another board."""
    sender_boards = S.sweep["gmail"]["sender_boards"]
    boards = crossmatch.waiting_boards(sender_boards, fulltext.blocked_hosts(S))
    assert boards == ("LinkedIn", "Pracuj.pl", "IrishJobs.ie", "Jobs.ie", "JustJoin IT",
                      "theprotocol.it")
    repo = FakeJobsRepo([
        linkedin_row(page_id="jj-1", Board="JustJoin IT"),
        linkedin_row(page_id="tp-1", Board="theprotocol.it", Role="DevOps Engineer (Remote)"),
        linkedin_row(page_id="nf-1", Board="NoFluffJobs"),  # read anyway: never filled here
    ])
    jj_alert = replace(ATS, source="gmail", board="JustJoin IT", description="",
                       url="https://justjoin.it/job-offer/vistula-cloud-devops-engineer")
    filled = crossmatch.fill_linkedin(repo, [jj_alert, NOFLUFF], CFG, None, set(),
                                      boards=boards)
    assert filled.labels == ["Vistula Cloud, DevOps Engineer (from NoFluffJobs)",
                             "Vistula Cloud, DevOps Engineer (Remote) (from NoFluffJobs)"]
    assert repo.read_body("jj-1")[0] == (
        "Description source: nofluffjobs (same job on NoFluffJobs, found for this JustJoin IT "
        "alert: https://nofluffjobs.com/pl/job/devops-engineer-vistula-cloud-warszawa)")
    assert repo.read_body("nf-1") == []


def test_justjoin_and_theprotocol_pages_are_never_opened():
    blocked = fulltext.blocked_hosts(S)
    assert "justjoin.it" in blocked and "theprotocol.it" in blocked
    opened = []
    guard = fulltext.SiteGuard(lambda url: opened.append(url) or (url, "<p>job</p>"), blocked)
    for url in ("https://justjoin.it/job-offer/acme-devops",
                "https://theprotocol.it/szczegoly/praca/devops-engineer,oferta,1"):
        with pytest.raises(http.HttpError):
            guard(url)
    guard("https://nofluffjobs.com/pl/job/devops")
    assert opened == ["https://nofluffjobs.com/pl/job/devops"]


def test_snippet_rows_get_the_full_description():
    """10 Oct: Adzuna rows hold a snippet only (screening says Needs review); the full text of
    the same job on another board is added after it, and screening reads the newest."""
    snippet = ["Description source: adzuna (snippet only)", "Short snippet."]
    repo = FakeJobsRepo([linkedin_row(page_id="adz-1", Board="Adzuna", body=list(snippet)),
                         linkedin_row(page_id="adz-2", Board="Adzuna",
                                      body=["Description source: adzuna", LONG])])
    filled = crossmatch.fill_linkedin(repo, [ATS], CFG, None, set(),
                                      boards=("LinkedIn", "Adzuna"))
    assert filled.labels == ["Vistula Cloud, DevOps Engineer (from Company site)"]
    body = repo.read_body("adz-1")
    assert body[:2] == snippet and body[2].startswith("Description source: ats (same job on "
                                                      "Company site, found for this Adzuna")
    assert repo.read_body("adz-2") == ["Description source: adzuna", LONG]  # already full
    from jobengine.screen.runner import description
    assert description(body) == (LONG, "full")
