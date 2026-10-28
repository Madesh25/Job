"""Direct job links (28 Oct): a LinkedIn or job board alert row gets the same job's own page on
the company's site, from the company's job board or one AI web search, never from LinkedIn. Only
the exact title on an employer page with a full description counts."""

from datetime import date

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.notion_repo import FakeJobsRepo
from jobengine.settings import load_settings
from jobengine.sweep import job_links
from jobengine.sweep.dedupe import link_rank
from jobengine.sweep.models import SweepSummary

TODAY = date(2026, 10, 28)
LINKEDIN = "https://www.linkedin.com/comm/jobs/view/4300000001?trackingId=x"
TEXT = "You build our cloud platform with Terraform, Kubernetes and AWS. " * 20
BOARDS = ("LinkedIn", "JustJoin IT")


def row(company="Dune Data", role="DevOps Engineer", city="Amsterdam",
        country="Netherlands", **extra):
    return {"Company": company, "Role": role, "City": city, "Country": country,
            "Board": "LinkedIn", "Status": "New", "Screen verdict": "Unscreened",
            "URL": LINKEDIN, "Posted date": TODAY, **extra}


def job_page(title, company="Dune Data", text=TEXT):
    return (f"<html><head><title>{title} | {company} Careers</title></head><body>"
            f"<h1>{title}</h1><div class='description'><p>{text}</p></div></body></html>")


class Pages:
    def __init__(self, pages):
        self.pages = pages
        self.opened = []

    def __call__(self, url):
        self.opened.append(url)
        if url not in self.pages:
            raise http.HttpError("GET failed: HTTP 404", status=404)
        return url, self.pages[url]


class Search:
    """Stands in for the AI web search."""

    def __init__(self, url="", sources=()):
        self.answer = {"url": url}
        self.sources = list(sources)
        self.calls = []

    def complete_json_with_search(self, stage, system, user, **kwargs):
        self.calls.append((stage, user))
        return self.answer, self.sources


def board_api(postings):
    """A Greenhouse board for "dune-data" with these (title, location, url) postings."""

    def get(url, params):
        if "boards-api.greenhouse.io/v1/boards/dunedata/jobs" in url or (
                "boards-api.greenhouse.io/v1/boards/dune-data/jobs" in url):
            return {"jobs": [{"id": i, "title": t, "location": {"name": loc}, "absolute_url": u,
                              "updated_at": "2026-10-27T10:00:00Z"}
                             for i, (t, loc, u) in enumerate(postings)]}
        raise http.HttpError("GET failed: HTTP 404", status=404)

    return get


def deps(rows, pages=None, get=None, llm=None, state=None):
    repo = FakeJobsRepo([{"page_id": f"p{i}", **r} for i, r in enumerate(rows)])
    return job_links.Deps(s=load_settings("local", {}), repo=repo, page=Pages(pages or {}),
                          get=get, llm=llm, state=state or FakeBotState(), today=TODAY)


def test_titles_and_places_must_match_exactly():
    assert job_links.same_title("DevOps Engineer (Remote)", "DevOps Engineer")
    assert not job_links.same_title("Senior DevOps Engineer", "DevOps Engineer")
    assert job_links.in_place("Amsterdam, North Holland", "Amsterdam", "Netherlands")
    assert job_links.in_place("Remote, NL", None, "Netherlands")
    assert not job_links.in_place("Berlin, Germany", "Amsterdam", "Netherlands")
    assert job_links.page_titles(job_page("SRE")) == ["SRE", "SRE"]


def test_employer_pages_only():
    assert job_links.employer_page("https://careers.dunedata.com/jobs/1", "Dune Data")
    assert not job_links.employer_page(LINKEDIN, "Dune Data")
    assert not job_links.employer_page("https://www.glassdoor.com/job/1", "Dune Data")
    assert not job_links.employer_page("https://justjoin.it/offers/1", "Dune Data")
    assert not job_links.employer_page("http://dunedata.com/jobs/1", "Dune Data")
    ats = "https://boards.greenhouse.io/other/jobs/1"
    assert not job_links.names_company(ats, job_page("SRE", company="Other Co"), "Dune Data")
    assert job_links.names_company(ats, job_page("SRE"), "Dune Data")


def test_web_search_result_is_read_and_checked_before_it_is_used():
    url = "https://careers.dunedata.com/jobs/42"
    search = Search(url, sources=["https://www.indeed.com/viewjob?jk=1"])
    d = deps([row()], pages={url: job_page("DevOps Engineer")}, llm=search)
    result = job_links.run(d, BOARDS)
    assert result.labels == ["Dune Data, DevOps Engineer (web search)"]
    values = d.repo.rows["p0"]
    assert values["URL"] == url and link_rank(values["URL"]) == 3
    body = values["body"]
    assert body[0] == f"Earlier link (LinkedIn): {LINKEDIN}"
    assert body[1].startswith("Description source: company site (direct link for this LinkedIn "
                              "job, found by web search")
    assert "Terraform" in body[2]
    assert search.calls[0][0] == "fetch" and "Dune Data" in search.calls[0][1]
    assert d.page.opened == [url]  # indeed is never opened, LinkedIn neither


def test_a_different_title_or_a_short_page_is_not_used():
    url = "https://careers.dunedata.com/jobs/42"
    for page in (job_page("Senior DevOps Engineer"), job_page("DevOps Engineer", text="Short.")):
        d = deps([row()], pages={url: page}, llm=Search(url))
        assert job_links.run(d, BOARDS).labels == []
        assert d.repo.rows["p0"]["URL"] == LINKEDIN and "body" not in d.repo.rows["p0"]


def test_the_company_job_board_comes_first_and_costs_no_search():
    url = "https://boards.greenhouse.io/dunedata/jobs/7"
    get = board_api([("Senior DevOps Engineer", "Amsterdam", "https://x.example/1"),
                     ("DevOps Engineer", "Amsterdam, Netherlands", url)])
    search = Search()
    d = deps([row()], pages={url: job_page("DevOps Engineer")}, get=get, llm=search)
    result = job_links.run(d, BOARDS)
    assert result.labels == ["Dune Data, DevOps Engineer (company job board)"]
    assert d.repo.rows["p0"]["URL"] == url
    assert search.calls == [] and result.searches == 0


def test_rows_are_not_tried_again_for_two_weeks_and_closed_rows_never():
    state = FakeBotState()
    search = Search()
    rows = [row(), row(company="Old Co", Status="Applied"), row(company="Skip Co",
            **{"Screen verdict": "Skip"}), row(company="Own Site", URL="https://ownsite.example/1")]
    d = deps(rows, llm=search, state=state)
    assert job_links.run(d, BOARDS).tried == 1
    assert job_links.run(d, BOARDS).tried == 0  # remembered
    later = job_links.Deps(**{**vars(d), "today": date(2026, 11, 12)})
    assert job_links.run(later, BOARDS).tried == 1


def test_search_budget_and_no_ai():
    d = deps([row(company=f"Co {i}") for i in range(5)], llm=Search())
    d.s.sweep["job_links"] = {"max_searches": 2}
    result = job_links.run(d, BOARDS)
    assert (result.tried, result.searches) == (5, 2)
    no_ai = deps([row()], llm=None)
    assert job_links.run(no_ai, BOARDS).searches == 0


def test_the_fetch_report_says_how_many_got_their_own_link():
    summary = SweepSummary(direct_links=["Dune Data, DevOps Engineer (web search)"],
                           direct_links_tried=3)
    text = summary.friendly_text()
    assert "Own job link found on the company's site" in text and "1 of 3" in text


def test_a_fetch_runs_the_link_search_for_the_alert_boards():
    from dataclasses import replace

    from test_telegram_bot import settings

    from jobengine.sweep.fakes import FAKE_TODAY
    from jobengine.sweep.runner import fake_deps, run_sweep

    s = settings()
    calls = []

    def links(repo, config, state, today, boards, with_body, progress):
        calls.append(boards)
        return job_links.Result(labels=["Dune Data, DevOps Engineer (web search)"], tried=2)

    summary = run_sweep(s, replace(fake_deps(s), links=links), FAKE_TODAY, state=FakeBotState())
    assert calls and "LinkedIn" in calls[0] and "Adzuna" in calls[0]
    assert summary.direct_links_tried == 2
    assert "1 of 2 alert jobs looked up" in summary.friendly_text()


def test_the_screen_price_follows_the_screening_model():
    from jobengine.config_store import ConfigStore
    from jobengine.screen.desk import SCREEN_COST_PER_JOB, screen_cost

    none = ConfigStore.from_values({})
    assert screen_cost(load_settings("dev", {}), none) == ("claude-haiku-4-5",
                                                            SCREEN_COST_PER_JOB)
    assert screen_cost(load_settings("local", {}), none) == ("gemini-3.5-flash-lite", 0.0006)
    model, cost = screen_cost(load_settings("prod", {}),
                              ConfigStore.from_values({"model.score": "deepseek-v4-flash"}))
    assert model == "deepseek-v4-flash" and 0 < cost < SCREEN_COST_PER_JOB


def test_a_company_without_a_job_board_is_not_asked_again():
    asked = []

    def get(url, params):
        asked.append(url)
        raise http.HttpError("GET failed: HTTP 404", status=404)

    state = FakeBotState()
    d = deps([row(), row(role="Platform Engineer")], get=get, state=state)
    job_links.run(d, BOARDS)
    first = len(asked)
    assert first and state.get(job_links.KEY)["no_board"] == {"dune data": "2026-10-28"}
    d2 = deps([row(role="Cloud Engineer")], get=get, state=state)
    job_links.run(d2, BOARDS)
    assert len(asked) == first  # remembered: no new job board questions
