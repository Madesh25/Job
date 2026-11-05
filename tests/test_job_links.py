"""Direct job links (28 Oct): a LinkedIn or job board alert row gets the same job's own page on
the company's site, from the company's job board or one AI web search, never from LinkedIn. Only
the exact title on an employer page with a full description counts."""

from datetime import date, timedelta

import pytest

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
    """Stands in for the AI web search: the job search answers `url`, the careers page
    search answers `careers`."""

    def __init__(self, url="", sources=(), careers=""):
        self.answer = {"url": url}
        self.sources = list(sources)
        self.careers = careers
        self.calls = []
        self.google = []

    def complete_json_with_search(self, stage, system, user, **kwargs):
        if system == job_links.GOOGLE_SYSTEM:  # 4 Nov: counted on its own
            self.google.append(user)
            return {"found": False}, []
        self.calls.append((stage, user))
        if system == job_links.SITE_SYSTEM:
            return {"careers": self.careers, "website": ""}, []
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


def deps(rows, pages=None, get=None, llm=None, state=None, careers=None):
    repo = FakeJobsRepo([{"page_id": f"p{i}", **r} for i, r in enumerate(rows)])
    return job_links.Deps(s=load_settings("local", {}), repo=repo, page=Pages(pages or {}),
                          get=get, llm=llm, state=state or FakeBotState(), today=TODAY,
                          careers=careers or {})


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
    assert search.calls[-1][0] == "fetch" and "Dune Data" in search.calls[-1][1]
    assert d.page.opened == [url]  # indeed is never opened, LinkedIn neither


def test_a_different_title_or_a_short_page_is_not_used():
    url = "https://careers.dunedata.com/jobs/42"
    for page in (job_page("Senior DevOps Engineer"), job_page("DevOps Engineer", text="Short.")):
        d = deps([row()], pages={url: page}, llm=Search(url))
        assert job_links.run(d, BOARDS).labels == []
        assert d.repo.rows["p0"]["URL"] == LINKEDIN
        # no description: only the note that the company's site could not be checked
        assert all(b.startswith("Company site check") for b in d.repo.rows["p0"]["body"])


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


def test_rows_are_tried_again_after_three_days_and_closed_rows_never():
    state = FakeBotState()
    search = Search()
    rows = [row(), row(company="Old Co", Status="Applied"), row(company="Skip Co",
            **{"Screen verdict": "Skip"}), row(company="Own Site", URL="https://ownsite.example/1")]
    d = deps(rows, llm=search, state=state)
    assert job_links.run(d, BOARDS).tried == 1
    assert job_links.run(d, BOARDS).tried == 0  # remembered
    next_day = job_links.Deps(**{**vars(d), "today": date(2026, 10, 29)})
    assert job_links.run(next_day, BOARDS).tried == 0
    later = job_links.Deps(**{**vars(d), "today": date(2026, 10, 31)})
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

    def links(repo, config, state, today, boards, with_body, progress, companies):
        calls.append(boards)
        assert companies  # Target Companies: their Careers URL comes first
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


CAREERS = "https://www.dunedata.com/careers"
JOB = "https://www.dunedata.com/careers/devops-engineer-amsterdam"


def careers_page(*links, form=""):
    items = "".join(f"<li><a href='{href}'><h3>{text}</h3><span>Amsterdam</span></a></li>"
                    for href, text in links)
    return (f"<html><head><title>Careers | Dune Data</title></head><body>"
            f"<nav><a href='/about'>About</a><a href='/vacancies'>All vacancies</a></nav>"
            f"{form}<ul>{items}</ul></body></html>")


def test_the_careers_page_of_target_companies_is_read_first_and_costs_no_search():
    """30 Oct, your request 2: open the company's careers page and take the job there."""
    search = Search()
    page = careers_page(("/careers/senior-devops-engineer", "Senior DevOps Engineer"),
                        ("/careers/devops-engineer-amsterdam", "DevOps Engineer"))
    d = deps([row()], pages={CAREERS: page, JOB: job_page("DevOps Engineer")}, llm=search,
             careers={"dune data": CAREERS})
    result = job_links.run(d, BOARDS)
    assert result.labels == ["Dune Data, DevOps Engineer (careers page)"]
    assert d.repo.rows["p0"]["URL"] == JOB
    assert search.calls == [] and result.searches == 0
    assert "https://www.dunedata.com/careers/senior-devops-engineer" not in d.page.opened


def test_the_careers_page_is_found_once_by_a_search_and_remembered():
    state = FakeBotState()
    search = Search(careers=CAREERS)
    page = careers_page(("/careers/devops-engineer-amsterdam", "DevOps Engineer"))
    pages = {CAREERS: page, JOB: job_page("DevOps Engineer")}
    d = deps([row()], pages=pages, llm=search, state=state)
    result = job_links.run(d, BOARDS)
    assert result.labels == ["Dune Data, DevOps Engineer (careers page)"]
    assert (result.searches, result.sites) == (1, 1)
    assert search.calls[0][1] == "Company: Dune Data\nCountry: Netherlands"
    assert state.get(job_links.KEY)["sites"]["dune data"]["url"] == CAREERS
    # a second job of the same company: no new search for the careers page, and with the
    # careers page read no search for the exact job either (30 Oct: it found 0 of 20)
    d2 = deps([row(), row(role="Platform Engineer")], pages=pages, llm=search, state=state)
    result = job_links.run(d2, BOARDS)
    assert len(search.calls) == 1
    # 4 Nov: a page with one job link is no job list, so its site counts as not checked:
    # only Google's copy is asked, once
    assert result.searches == 1 and len(search.google) == 1


def test_a_careers_page_answer_of_another_company_is_not_used():
    other = "https://www.otherco.com/careers"
    d = deps([row()], pages={other: careers_page()}, llm=Search(careers=other))
    job_links.run(d, BOARDS)
    assert d.state.get(job_links.KEY)["sites"]["dune data"]["url"] == ""


def test_the_careers_page_search_box_is_used_like_by_hand():
    form = ("<form action='/careers/search' method='get'><input type='hidden' name='lang' "
            "value='en'><input type='text' name='q'><button>Search</button></form>")
    found = careers_page(("/careers/devops-engineer-amsterdam", "DevOps Engineer"))
    asked = "https://www.dunedata.com/careers/search?lang=en&q=DevOps+Engineer"
    d = deps([row()], pages={CAREERS: careers_page(form=form), asked: found,
                             JOB: job_page("DevOps Engineer")}, careers={"dune data": CAREERS})
    assert job_links.run(d, BOARDS).labels == ["Dune Data, DevOps Engineer (careers page search)"]
    assert d.page.opened[:2] == [CAREERS, asked]


def test_forms_that_send_or_log_in_are_never_used():
    post = "<form action='/apply' method='post'><input type='text' name='q'></form>"
    login = ("<form action='/login'><input type='text' name='q'>"
             "<input type='password' name='p'></form>")
    for form in (post, login):
        assert job_links.search_url(CAREERS, careers_page(form=form), "DevOps") is None


def test_a_job_list_page_linked_from_the_careers_page_is_read():
    listing = "https://www.dunedata.com/vacancies"
    found = careers_page(("/careers/devops-engineer-amsterdam", "DevOps Engineer"))
    d = deps([row()], pages={CAREERS: careers_page(), listing: found,
                             JOB: job_page("DevOps Engineer")}, careers={"dune data": CAREERS})
    assert job_links.run(d, BOARDS).labels == ["Dune Data, DevOps Engineer (careers page)"]


def test_a_careers_page_that_uses_a_job_board_is_read_through_it():
    url = "https://boards.greenhouse.io/dunedata/jobs/7"
    page = ("<html><body><a href='https://boards.greenhouse.io/dunedata'>Open roles</a>"
            "<iframe src='https://boards.greenhouse.io/embed/job_board?for=dunedata'>"
            "</iframe></body></html>")
    get = board_api([("DevOps Engineer", "Amsterdam", url)])
    d = deps([row(company="Dune Data BV")], pages={CAREERS: page, url: job_page("DevOps Engineer")},
             get=get, careers={"dune data": CAREERS})
    assert job_links.run(d, BOARDS).labels == ["Dune Data BV, DevOps Engineer (company job board)"]


def test_smartrecruiters_is_asked_for_the_title():
    """ALTEN, 30 Oct: a big company's board lists hundreds of jobs; ask for the title."""
    asked = []
    url = "https://jobs.smartrecruiters.com/alten/7"

    def get(u, params):
        asked.append((u, dict(params or {})))
        if u == "https://api.smartrecruiters.com/v1/companies/alten/postings":
            return {"totalFound": 1, "content": [{
                "id": "7", "name": "DevOps Engineer", "releasedDate": "2026-10-27T10:00:00Z",
                "location": {"city": "Warsaw", "country": "pl"}}]}
        raise http.HttpError("GET failed: HTTP 404", status=404)

    d = deps([row(company="ALTEN Polska", city="Warsaw", country="Poland")],
             pages={url: job_page("DevOps Engineer", company="ALTEN")}, get=get)
    assert job_links.run(d, BOARDS).labels == ["ALTEN Polska, DevOps Engineer (company job board)"]
    reads = [p for u, p in asked if u.endswith("/alten/postings") and "offset" in p]
    assert reads and all(p["q"] == "DevOps Engineer" for p in reads)


def test_target_companies_give_their_careers_links():
    from jobengine.sweep.models import TargetCompany

    companies = [TargetCompany("Dune Data (NL)", CAREERS, "Custom", True, "NL"),
                 TargetCompany("Dune Data (PL)", "https://www.dunedata.pl/kariera", "Custom",
                               True, "PL"),
                 TargetCompany("Acme", "", "Unknown", True, None)]
    assert job_links.careers_of(companies) == {"dune data": CAREERS}


HOME = "https://www.dunedata.com/"


def home_page(*links):
    anchors = "".join(f"<a href='{href}'>{text}</a>" for href, text in links)
    return f"<html><body><nav>{anchors}</nav><p>Dune Data builds data tools.</p></body></html>"


def test_from_the_home_page_two_clicks_to_the_job():
    """30 Oct: the search gave home pages (ASTEK, Sii, CGI); the jobs were two clicks away."""
    careers = "https://www.dunedata.com/werken-bij"
    listing = "https://www.dunedata.com/werken-bij/vacatures"
    pages = {HOME: home_page(("/werken-bij", "Werken bij")),
             careers: home_page(("/werken-bij/vacatures", "Alle vacatures")),
             listing: careers_page(("/careers/devops-engineer-amsterdam", "DevOps Engineer")),
             JOB: job_page("DevOps Engineer")}
    d = deps([row()], pages=pages, careers={"dune data": HOME})
    assert job_links.run(d, BOARDS).labels == ["Dune Data, DevOps Engineer (careers page)"]
    assert d.page.opened == [HOME, careers, listing, JOB]


def test_links_to_other_sites_are_not_followed():
    pages = {HOME: home_page(("https://www.irishjobs.ie/jobs", "Our jobs"),
                             ("https://www.other.com/careers", "Careers at Other"))}
    d = deps([row()], pages=pages, careers={"dune data": HOME})
    job_links.run(d, BOARDS)
    assert d.page.opened == [HOME]


def test_a_javascript_careers_page_is_opened_in_the_browser():
    """Cognizant, Emerging Travel Group (30 Oct): the jobs list comes from JavaScript."""
    shell = "<html><body><div id='app'>Loading jobs...</div></body></html>"
    rendered = careers_page(("/careers/devops-engineer-amsterdam", "DevOps Engineer"))
    d = deps([row()], pages={CAREERS: shell, JOB: job_page("DevOps Engineer")},
             careers={"dune data": CAREERS})
    browser = Pages({CAREERS: rendered})
    d.render = browser
    assert job_links.run(d, BOARDS).labels == ["Dune Data, DevOps Engineer (careers page)"]
    assert browser.opened == [CAREERS]


def test_a_home_page_is_not_opened_in_the_browser():
    d = deps([row()], pages={HOME: home_page()}, careers={"dune data": HOME})
    browser = Pages({})
    d.render = browser
    job_links.run(d, BOARDS)
    assert browser.opened == []


def workday_api(tenant, shard, site, jobs):
    """A Workday site on one server: the job list answers, every other server fails."""
    posted = []

    def post(url, body):
        posted.append((url, body))
        if url == f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs":
            return {"total": len(jobs), "facets": [], "jobPostings": [
                {"title": title, "externalPath": f"/job/{place}/{path}",
                 "locationsText": place, "postedOn": "Posted Today"}
                for title, place, path in jobs]}
        raise http.HttpError("POST failed: ConnectError")

    return post, posted


def test_a_workday_link_without_its_server_is_repaired():
    """Armacell, Coca-Cola (30 Oct): the AI gave armacell.myworkdayjobs.com, which does
    not exist; the real site is armacell.wd3.myworkdayjobs.com."""
    post, posted = workday_api("armacell", "wd3", "Armacell",
                               [("Systems Engineer", "Wroclaw, Poland", "Systems-Engineer_JR-1")])
    shardless = "https://armacell.myworkdayjobs.com/Armacell/job/Wroclaw/Systems-Engineer_JR-2"
    real = "https://armacell.wd3.myworkdayjobs.com/en-US/Armacell/job/Systems-Engineer_JR-1"

    def get(url, params):  # the job's detail: its own link and the full description
        if url.endswith("/Systems-Engineer_JR-1"):
            return {"jobPostingInfo": {"title": "Systems Engineer", "externalUrl": real,
                                       "location": "Wroclaw, Poland",
                                       "jobDescription": f"<p>{TEXT}</p>"}}
        raise http.HttpError("GET failed: HTTP 404", status=404)

    d = deps([row(company="Armacell", role="Systems Engineer", city="Wroclaw", country="Poland")],
             get=get, llm=Search(shardless))
    d.post = post
    assert job_links.workday_board(d, shardless).host == "armacell.wd3.myworkdayjobs.com"
    result = job_links.run(d, BOARDS)
    assert d.repo.rows["p0"]["URL"] == real
    assert result.labels == ["Armacell, Systems Engineer (web search)"]
    searched = [b["searchText"] for u, b in posted if "wd3" in u and b.get("searchText")]
    assert "Systems Engineer" in searched  # the site's own search, for this title


def test_companies_are_looked_up_at_the_same_time():
    """30 Oct, your idea: three companies at once; one company's jobs stay together."""
    import threading
    import time

    running, peak, lock = [0], [0], threading.Lock()
    by_company: dict[str, list[str]] = {}

    class SlowSearch(Search):
        def complete_json_with_search(self, stage, system, user, **kwargs):
            company = user.split("\n")[0]
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
                by_company.setdefault(company, []).append(threading.current_thread().name)
            time.sleep(0.05)
            with lock:
                running[0] -= 1
            return super().complete_json_with_search(stage, system, user, **kwargs)

    rows = [row(company=f"Co {i}", role=role) for i in range(6)
            for role in ("DevOps Engineer", "Cloud Engineer")]
    d = deps(rows, llm=SlowSearch())
    d.s.sweep["job_links"]["workers"] = 3  # prod's number (local and dev: 5)
    result = job_links.run(d, BOARDS)
    assert result.tried == 12 and peak[0] == 3
    # one careers page search per company, never two at once for the same company
    assert all(len(set(threads)) == 1 for threads in by_company.values())
    assert sum(1 for c in d.llm.calls if "Job title" not in c[1]) == 6


def test_workers_one_keeps_the_old_order():
    d = deps([row(company=f"Co {i}") for i in range(3)], llm=Search())
    d.s.sweep["job_links"] = {"workers": 1}
    job_links.run(d, BOARDS)
    assert [c[1].split("\n")[0] for c in d.llm.calls][::2] == [
        "Company: Co 0", "Company: Co 1", "Company: Co 2"]


def test_a_job_system_link_in_target_companies_is_read_as_a_job_board():
    """HCLTech (30 Oct): its Careers URL is a job feed; it is read as one, not as a page."""
    url = "https://boards.greenhouse.io/dunedata/jobs/7"
    get = board_api([("DevOps Engineer", "Amsterdam", url)])
    # its name is not the board's name, so only the Careers URL leads there
    d = deps([row(company="DD Group")], pages={url: job_page("DevOps Engineer")}, get=get,
             careers={job_links.company_key("DD Group"): "https://boards.greenhouse.io/dunedata"})
    assert job_links.run(d, BOARDS).labels == ["DD Group, DevOps Engineer (company job board)"]
    assert d.page.opened == [url]  # the job itself, never the board link as a page


def test_rows_without_a_real_company_are_not_looked_up():
    """30 Oct: LinkedIn alert rows with the company "1" and "Hybrid"."""
    search = Search()
    d = deps([row(company="1"), row(company="Hybrid"), row(company="Dune Data")], llm=search)
    assert job_links.run(d, BOARDS).tried == 1
    assert all("Dune Data" in c[1] for c in search.calls)
    assert job_links.real_company("ING") and job_links.real_company("3M")
    assert not job_links.real_company("Remote") and not job_links.real_company("42")


def long_page(*links):
    """A long careers page full of text and links (CGI's werken-bij pages, 30 Oct)."""
    nav = "".join(f"<a href='/info/{i}'>Info {i}</a>" for i in range(40))
    return (f"<html><body><nav>{nav}</nav><p>{TEXT * 3}</p>"
            f"{careers_page(*links)}</body></html>")


def test_a_long_page_that_lists_jobs_is_not_opened_in_the_browser():
    """30 Oct: CGI's long pages used 5 of the 15 browser pages and found nothing. A long
    page that already shows a job list (without this job) is not opened again."""
    listed = long_page(("/careers/java-developer", "Java Developer"),
                       ("/careers/data-engineer", "Data Engineer"),
                       ("/careers/frontend-engineer", "Frontend Engineer"))
    d = deps([row()], pages={CAREERS: listed}, careers={"dune data": CAREERS})
    browser = Pages({})
    d.render = browser
    job_links.run(d, BOARDS)
    assert browser.opened == []


def test_a_long_page_with_no_job_list_is_opened_in_the_browser():
    """4 Nov: YachtCloud's careers page is full of menus and text, and its job list comes
    later by JavaScript: it was never opened, so its job was never found."""
    rendered = careers_page(("/careers/devops-engineer-amsterdam", "DevOps Engineer"))
    d = deps([row()], pages={CAREERS: long_page(), JOB: job_page("DevOps Engineer")},
             careers={"dune data": CAREERS})
    browser = Pages({CAREERS: rendered})
    d.render = browser
    assert job_links.run(d, BOARDS).labels == ["Dune Data, DevOps Engineer (careers page)"]
    assert browser.opened == [CAREERS]


def test_one_job_opens_at_most_two_pages_in_the_browser():
    shell = "<html><body><div id='app'></div><a href='/careers/list'>Jobs</a></body></html>"
    listing = "https://www.dunedata.com/careers/list"
    d = deps([row()], pages={CAREERS: shell, listing: shell}, careers={"dune data": CAREERS})
    browser = Pages({CAREERS: shell, listing: shell})
    browser.left = 40
    d.render = browser
    job_links.run(d, BOARDS)
    assert len(browser.opened) == 2


def test_a_job_cut_short_by_the_budget_is_looked_up_next_sweep():
    """30 Oct: the browser pages ran out at 10:52; Sii, ANWB, Randstad and others then
    waited 14 days although they were never really looked up."""
    shell = "<html><body><div id='app'>Loading...</div></body></html>"
    state = FakeBotState()
    d = deps([row()], pages={CAREERS: shell}, careers={"dune data": CAREERS}, state=state)
    browser = Pages({})
    browser.left = 0  # this /fetch's browser pages are used up
    d.render = browser
    result = job_links.run(d, BOARDS)
    assert result.again == 1 and "p0" not in state.get(job_links.KEY)["tried"]
    # the same when the searches ran out: one search for two companies, so the first
    # misses its job search and the second its careers page search
    d2 = deps([row(company="Other Co"), row(company="Third Co")], llm=Search(),
              state=FakeBotState())
    d2.s.sweep["job_links"] = {"max_searches": 1, "workers": 1}
    result = job_links.run(d2, BOARDS)
    assert result.again == 2 and result.tried == 2 and result.searches == 1
    # without the AI nothing "runs out": the jobs are remembered as usual
    d3 = deps([row(company="Other Co")], llm=None, state=FakeBotState())
    assert job_links.run(d3, BOARDS).again == 0


def test_the_summary_says_how_many_wait_for_the_next_fetch():
    summary = SweepSummary(direct_links=[], direct_links_tried=40, direct_links_again=12)
    assert "12 not finished" in summary.friendly_text()


def test_a_slow_source_is_left_out_and_the_fetch_goes_on(monkeypatch):
    import threading
    from dataclasses import replace

    from test_telegram_bot import settings

    from jobengine import parallel
    from jobengine.sweep.fakes import FAKE_TODAY
    from jobengine.sweep.models import SourceResult
    from jobengine.sweep.runner import fake_deps, run_sweep

    monkeypatch.setattr(parallel, "TICK_SECONDS", 0.02)
    release = threading.Event()

    def stuck():
        release.wait(5)
        return SourceResult(name="jooble")

    s = settings()
    s.sweep["source_minutes"] = 0.005  # 0.3 seconds
    summary = run_sweep(s, replace(fake_deps(s), jooble=stuck, links=None), FAKE_TODAY,
                        sources=("gmail", "jooble"))
    release.set()
    assert "Jooble: too slow this time" in summary.not_checked
    assert summary.sources["gmail"] > 0  # the other source was used


def test_an_at_company_ending_does_not_stop_the_match():
    """30 Oct: Adzuna titles end with the company ("Senior DevOps Consultant @ GFT Poland",
    69 rows); the company's own page has the title without it."""
    from jobengine.sweep.crossmatch import plain_title, title_key

    assert title_key("DevOps Engineer @ Scalo") == title_key("DevOps Engineer")
    assert plain_title("Junior DevOps Engineer @ AVENGA (Agencja Pracy, nr KRAZ: 8448)") == (
        "Junior DevOps Engineer")
    form = ("<form action='/careers/search' method='get'><input type='text' name='q'></form>")
    found = careers_page(("/careers/devops-engineer-amsterdam", "DevOps Engineer"))
    asked = "https://www.dunedata.com/careers/search?q=DevOps+Engineer"
    d = deps([row(role="DevOps Engineer @ Dune Data")],
             pages={CAREERS: careers_page(form=form), asked: found,
                    JOB: job_page("DevOps Engineer")}, careers={"dune data": CAREERS})
    result = job_links.run(d, BOARDS)
    assert result.labels == ["Dune Data, DevOps Engineer (careers page search)"]
    assert asked in d.page.opened  # the search box gets the title without "@ Dune Data"


def test_the_log_names_the_closest_titles_seen(caplog):
    """30 Oct: 14 of 40 careers pages were read without the exact title; the log now says
    which titles came close, so a missing job and a renamed one can be told apart."""
    import logging

    page = careers_page(("/careers/1", "Senior DevOps Engineer"),
                        ("/careers/2", "Junior/Mid DevOps Engineer"),
                        ("/careers/3", "Office Manager"))
    d = deps([row()], pages={CAREERS: page}, careers={"dune data": CAREERS})
    with caplog.at_level(logging.INFO, logger="jobengine.sweep"):
        job_links.run(d, BOARDS)
    line = next(r.getMessage() for r in caplog.records if "no direct link" in r.getMessage())
    assert "closest titles seen: " in line
    assert "'Senior DevOps Engineer'" in line and "'Junior/Mid DevOps Engineer'" in line
    assert "Office Manager" not in line


def test_seniority_words_and_job_boards_are_not_companies():
    for name in ("Mid/Senior", "Junior", "SOLID.Jobs", "GamblingCareers.com"):
        assert not job_links.real_company(name), name
    assert job_links.real_company("Seniorita Foods")







def test_a_job_number_or_button_text_on_a_careers_page_is_not_part_of_the_title():
    """2 Nov log: STAFIDE "DevOps / Platform Engineer (ID:4116)" and wolk "Platform / DevOps
    Engineer Explore this vacancy" were the jobs, but did not count."""
    assert job_links.same_title("DevOps / Platform Engineer (ID:4116)",
                                "DevOps / Platform Engineer")
    assert job_links.same_title("Platform / DevOps Engineer Explore this vacancy",
                                "Platform/DevOps Engineer")
    assert job_links.same_title("Cloud Engineer Bekijk deze vacature →", "Cloud Engineer")
    assert job_links.same_title("DevOps Engineer [Ref: SR-221]", "DevOps Engineer")
    # still the exact title
    assert not job_links.same_title("Senior DevOps / Platform Engineer (ID: 4028)",
                                    "DevOps / Platform Engineer")
    assert not job_links.same_title("Apply Engineer", "Engineer")


def test_a_job_board_the_bot_may_not_open_does_not_stop_the_job():
    """2 Nov: HSBC and Qualcomm (Eightfold) raised a safety error and were not looked up."""
    from jobengine.safety import SafetyError

    def refuse(url, params):
        raise SafetyError("refusing to fetch app.eightfold.ai: host is not on "
                          "safety.allowed_hosts")

    from jobengine.sweep.sources.ats import Board

    board = Board("eightfold", "qualcomm", host="app.eightfold.ai")
    d = deps([row()], get=refuse)
    assert job_links._board_postings(d, "Qualcomm", board, "DevOps Engineer") == []


def test_dev_looks_up_five_companies_at_a_time_and_prod_three():
    """2 Nov, your decision: faster DEV fetches; prod keeps the recommended 3."""
    assert load_settings("local", {}).sweep["job_links"]["workers"] == 5
    assert load_settings("dev", {}).sweep["job_links"]["workers"] == 5
    assert load_settings("prod", {}).sweep["job_links"]["workers"] == 3


def test_a_team_code_or_running_number_is_not_part_of_the_title():
    """2 Nov, second log: Software Mind "[WBG] DevOps Engineer", Infolet "Platform Engineer
    (18/22)"."""
    assert job_links.same_title("[WBG] DevOps Engineer", "DevOps Engineer")
    assert job_links.same_title("Platform Engineer (18/22)", "Platform Engineer (Remote)")
    assert not job_links.same_title("[8BE] Senior DevOps Engineer (AWS)", "DevOps Engineer")


def test_a_job_link_with_http_is_opened_with_https():
    """2 Nov: Xccelerated links its jobs with http://, which the bot never opens."""
    html = '<a href="http://www.xccelerated.io/jobs/cloud-engineer">Cloud Engineer</a>'
    assert job_links.title_links("https://www.xccelerated.io/careers", html,
                                 "Cloud Engineer", None, "Netherlands") == [
        "https://www.xccelerated.io/jobs/cloud-engineer"]


def test_dev_opens_a_browser_page_per_job_looked_up():
    assert load_settings("local", {}).sweep["job_links"]["max_rendered"] == 80
    assert load_settings("prod", {}).sweep["job_links"]["max_rendered"] == 40



GREENHOUSE_JOBS = "https://boards-api.greenhouse.io/v1/boards/dunedata/jobs"


def board_get(titles):
    """The company's Greenhouse board by its name, listing these jobs."""
    jobs = [{"id": i, "title": t, "absolute_url": f"https://boards.greenhouse.io/dunedata/{i}",
             "location": {"name": "Amsterdam"}, "content": TEXT, "updated_at": "2026-10-27"}
            for i, t in enumerate(titles)]

    def get(url, params=None):
        if "greenhouse" in url and "dunedata" in url:
            return {"jobs": jobs}
        raise http.HttpError("GET failed: HTTP 404", status=404)
    return get


def test_a_job_not_on_the_company_job_list_is_skipped_at_the_second_check():
    """2 Nov, your rule: a job the company does not put on its own site is likely a ghost
    job. First miss: a note and a look again after RETRY_DAYS; second miss: skipped."""
    state = FakeBotState()
    d = deps([row()], get=board_get(["Data Engineer"]), state=state)
    result = job_links.run(d, BOARDS)
    assert (result.not_yet, result.ghosts, result.unchecked) == (1, [], 0)
    page = d.repo.rows["p0"]
    assert page["Screen verdict"] == "Unscreened"
    assert page["body"][-1].startswith("Company site check (2026-10-28): not on Dune Data's "
                                       "own job list yet")
    later = deps([row()], get=board_get(["Data Engineer"]), state=state)
    later.repo, later.today = d.repo, TODAY + timedelta(days=job_links.RETRY_DAYS + 1)
    result = job_links.run(later, BOARDS)
    assert result.ghosts == ["Dune Data, DevOps Engineer"]
    assert page["Screen verdict"] == "Skip"
    assert "checked 2026-10-28, 2026-11-01" in page["body"][-1]
    assert "likely a ghost job" in page["body"][-1] and "LinkedIn" in page["body"][-1]
    # skipped rows are not looked up again
    again = deps([row()], get=board_get(["Data Engineer"]), state=state)
    again.repo, again.today = d.repo, TODAY + timedelta(days=10)
    assert job_links.run(again, BOARDS).tried == 0


def test_a_screened_row_is_never_skipped_by_the_check():
    state = FakeBotState()
    for day in (0, job_links.RETRY_DAYS + 1):
        d = deps([row(**{"Screen verdict": "Needs review", "Status": "Screened"})],
                 get=board_get(["Data Engineer"]), state=state)
        d.today = TODAY + timedelta(days=day)
        result = job_links.run(d, BOARDS)
    assert result.ghosts == ["Dune Data, DevOps Engineer"]
    assert d.repo.rows["p0"]["Screen verdict"] == "Needs review"


def test_a_site_that_could_not_be_checked_is_noted_once_and_waits_for_jd():
    state = FakeBotState()
    d = deps([row()], state=state)  # no job board, no careers page, no AI
    result = job_links.run(d, BOARDS)
    assert (result.unchecked, result.not_yet, result.ghosts) == (1, 0, [])
    assert d.repo.rows["p0"]["Screen verdict"] == "Unscreened"
    assert "could not be checked" in d.repo.rows["p0"]["body"][-1]
    later = deps([row()], state=state)
    later.repo, later.today = d.repo, TODAY + timedelta(days=job_links.RETRY_DAYS + 1)
    job_links.run(later, BOARDS)
    assert len(d.repo.rows["p0"]["body"]) == 1  # noted once


def test_the_note_is_never_read_as_a_description():
    from jobengine.screen.runner import description

    body = ["Description source: adzuna (snippet only)", "Short snippet.",
            "Company site check (2026-11-02): not on Acme's own job list yet."]
    assert description(body, 10) == ("Short snippet.", "snippet")


def test_a_job_page_in_another_country_is_not_the_job():
    """2 Nov: AgileEngine's India job was taken for a Poland row."""
    assert job_links.other_country(
        "https://join.agileengine.com/open-positions/devops-engineer-kobie-india-id98058/",
        "<html></html>", "Poland")
    assert not job_links.other_country("https://acme.com/jobs/devops-engineer-krakow",
                                       "<html></html>", "Poland")
    assert not job_links.other_country("https://acme.com/join-us/devops", "", "Ireland")
    ld = ('<script type="application/ld+json">{"@type": "JobPosting", "jobLocation": '
          '{"address": {"addressCountry": "%s"}}}</script>')
    assert job_links.other_country("https://acme.com/jobs/1", ld % "IN", "Poland")
    assert not job_links.other_country("https://acme.com/jobs/1", ld % "PL", "Poland")


def test_the_summary_says_what_happened_to_the_jobs_not_found():
    text = SweepSummary(direct_links_tried=80, not_on_site_yet=12, site_unchecked=9,
                        ghost_jobs=["Acme, DevOps Engineer"]).friendly_text()
    assert "12 not on the company's own job list yet: looked up again in 3 days" in text
    assert "9 whose company site could not be checked" in text
    assert ("Not on the company's own site at two checks, skipped as likely ghost jobs "
            "(noted on each page): 1") in text


def test_a_board_guessed_by_name_without_jobs_in_the_country_is_no_check():
    """Another company may have the same name: its board is not this company's job list."""
    def get(url, params=None):
        if "greenhouse" in url and "dunedata" in url:
            return {"jobs": [{"id": 1, "title": "Data Engineer", "content": TEXT,
                              "absolute_url": "https://boards.greenhouse.io/dunedata/1",
                              "location": {"name": "Austin, Texas"},
                              "updated_at": "2026-10-27"}]}
        raise http.HttpError("GET failed: HTTP 404", status=404)

    result = job_links.run(deps([row()], get=get), BOARDS)
    assert (result.unchecked, result.not_yet) == (1, 0)


def list_page(*titles):
    """A careers page that lists jobs (links with job titles)."""
    return careers_page(*[(f"/careers/{i}", t) for i, t in enumerate(titles)])


def test_a_homepage_or_a_missing_careers_page_is_no_job_list():
    """2 Nov: Grape Up's homepage and Natek's 404 careers pages counted as a job list."""
    home = "<html><body><a href='/about'>About us</a><a href='/blog'>Blog</a></body></html>"
    d = deps([row()], pages={CAREERS: home}, careers={"dune data": CAREERS})
    result = job_links.run(d, BOARDS)
    assert (result.unchecked, result.not_yet) == (1, 0)
    lists = deps([row()], pages={CAREERS: list_page("Data Engineer", "QA Engineer",
                                                     "Product Manager")},
                 careers={"dune data": CAREERS})
    result = job_links.run(lists, BOARDS)
    assert (result.unchecked, result.not_yet) == (0, 1)
    assert job_links.job_list_page(list_page("Data Engineer", "QA Engineer", "Analyst"))
    assert not job_links.job_list_page(home)


def test_a_job_on_the_list_whose_page_cannot_be_read_is_never_a_ghost():
    """2 Nov: Technopride lists "Microsoft Azure Cloud Engineer", but its job page needs the
    browser, and the browser pages had run out."""
    page = list_page("DevOps Engineer", "Data Engineer", "QA Engineer")
    d = deps([row()], pages={CAREERS: page}, careers={"dune data": CAREERS})
    result = job_links.run(d, BOARDS)
    assert (result.unchecked, result.not_yet, result.ghosts) == (1, 0, [])
    assert "the job is on its job list, but its page could not be read" in (
        d.repo.rows["p0"]["body"][-1])


def test_a_job_page_built_by_javascript_is_opened_in_the_browser():
    job_url = "https://www.dunedata.com/careers/0"
    empty = "<html><head><title>DevOps Engineer | Dune Data</title></head><body></body></html>"
    page = list_page("DevOps Engineer", "Data Engineer", "QA Engineer")
    d = deps([row()], pages={CAREERS: page, job_url: empty}, careers={"dune data": CAREERS})

    class Browser:
        left = 5

        def __call__(self, url):
            self.left -= 1
            return url, job_page("DevOps Engineer")

    d.render = Browser()
    result = job_links.run(d, BOARDS)
    assert result.labels == ["Dune Data, DevOps Engineer (careers page)"]


def test_a_city_name_or_a_job_number_on_the_title_is_not_part_of_it():
    """2 Nov: Netcompany "Junior Cloud DevOps Engineer (Azure) (f/m) Warsaw" for a row in
    Warszawa; AgileEngine's alert title "GCP Cloud Engineer ID89421"."""
    assert job_links.same_title("Junior Cloud DevOps Engineer (Azure) (f/m) Warsaw",
                                "Junior Cloud DevOps Engineer (Azure)", "Warszawa", "Poland")
    assert job_links.same_title("GCP Cloud Engineer", "GCP Cloud Engineer ID89421", None,
                                "Poland")
    assert not job_links.same_title("Junior Cloud DevOps Engineer (Azure) Athens",
                                    "Junior Cloud DevOps Engineer (Azure)", "Warszawa", "Poland")


def test_the_checks_of_the_first_run_are_not_counted():
    """The 2 Nov run counted homepages as job lists: its misses are dropped."""
    state = FakeBotState()
    state.set(job_links.KEY, {"misses": {"p0": ["2026-10-27"]}})
    d = deps([row()], get=board_get(["Data Engineer"]), state=state)
    result = job_links.run(d, BOARDS)
    assert result.ghosts == [] and result.not_yet == 1


def test_a_guessed_board_whose_jobs_have_no_place_is_no_check():
    """2 Nov: "greenhouse link" was taken as Link Group's job list."""
    def get(url, params=None):
        if "greenhouse" in url and "dunedata" in url:
            return {"jobs": [{"id": 1, "title": "Data Engineer", "content": TEXT,
                              "absolute_url": "https://boards.greenhouse.io/dunedata/1",
                              "location": {"name": ""}, "updated_at": "2026-10-27"}]}
        raise http.HttpError("GET failed: HTTP 404", status=404)

    result = job_links.run(deps([row()], get=get), BOARDS)
    assert (result.unchecked, result.not_yet) == (1, 0)


def test_a_description_already_on_the_page_is_not_written_again():
    """2 Nov: Snowflake's description from its job board was written twice (cross-match,
    then the direct link of the same job)."""
    page = list_page("DevOps Engineer", "Data Engineer", "QA Engineer")
    job_url = "https://www.dunedata.com/careers/0"
    body = [f"Description source: ats (same job on Company site: {job_url})", TEXT]
    d = deps([row(body=body)], pages={CAREERS: page, job_url: job_page("DevOps Engineer")},
             careers={"dune data": CAREERS})
    result = job_links.run(d, BOARDS)
    assert result.labels == ["Dune Data, DevOps Engineer (careers page)"]
    assert d.repo.rows["p0"]["URL"] == job_url and d.repo.rows["p0"]["body"] == body


def test_a_board_without_jobs_in_the_country_is_no_check():
    """2 Nov: Capgemini's board listed Milan, Singapore and Atlanta for a Dutch job."""
    def get(url, params=None):
        if "greenhouse" in url and "dunedata" in url:
            return {"jobs": [{"id": 1, "title": "DevOps Engineer", "content": TEXT,
                              "absolute_url": "https://boards.greenhouse.io/dunedata/1",
                              "location": {"name": "Milano, IT"}, "updated_at": "2026-10-27"}]}
        raise http.HttpError("GET failed: HTTP 404", status=404)

    d = deps([row()], get=get, pages={CAREERS: "<html></html>"},
             careers={"dune data": "https://boards.greenhouse.io/dunedata"})
    result = job_links.run(d, BOARDS)
    assert (result.unchecked, result.not_yet) == (1, 0)


def test_a_row_without_a_company_is_noted_once_and_skipped():
    """2 Nov: the same 7 rows ("Hybrid", "1", "Mid/Senior", "(unknown)") came back in every
    /fetch log: their company's site can never be checked."""
    state = FakeBotState()
    screened = row(company="(unknown)", **{"Screen verdict": "Needs review",
                                           "Status": "Screened"})
    d = deps([row(company="Hybrid"), screened], state=state)
    result = job_links.run(d, BOARDS)
    assert result.no_company == ["DevOps Engineer ('Hybrid')", "DevOps Engineer ('(unknown)')"]
    assert result.tried == 0
    hybrid, unknown = d.repo.rows["p0"], d.repo.rows["p1"]
    assert hybrid["Screen verdict"] == "Skip" and "showed 'Hybrid'" in hybrid["body"][-1]
    assert unknown["Screen verdict"] == "Needs review" and len(unknown["body"]) == 1
    again = deps([row(company="Hybrid"), screened], state=state)
    again.repo = d.repo
    assert job_links.run(again, BOARDS).no_company == []  # noted once
    assert len(unknown["body"]) == 1
    text = SweepSummary(direct_links_tried=1, no_company=result.no_company).friendly_text()
    assert "Alert jobs without a company name (noted on each page" in text


def test_a_board_known_to_miss_jobs_is_no_check():
    """2 Nov: Capgemini's board did not list its Dutch jobs (the summary's 'job board did not
    list' line); those jobs must not become ghost jobs."""
    state = FakeBotState()
    job_links.note_incomplete(state, ["Dune Data"], TODAY - timedelta(days=5))
    d = deps([row()], get=board_get(["Data Engineer"]), state=state)
    result = job_links.run(d, BOARDS)
    assert (result.unchecked, result.not_yet) == (1, 0)
    assert "does not list all its jobs" in d.repo.rows["p0"]["body"][-1]
    job_links.note_incomplete(state, [], TODAY + timedelta(days=job_links.INCOMPLETE_DAYS))
    assert job_links._incomplete(state, TODAY + timedelta(days=job_links.INCOMPLETE_DAYS)) == set()


def test_a_job_list_that_needs_the_browser_with_no_page_left_is_no_check():
    """2 Nov: Infor's job list (Pinpoint) needs the browser; this job's pages had run out."""
    thin = ("<html><body><a href='/careers/jobs'>Jobs</a>"
            + "".join(f"<a href='/p{i}'>Senior Engineer {i}</a>" for i in range(5))
            + "</body></html>")
    d = deps([row()], pages={CAREERS: thin}, careers={"dune data": CAREERS})

    class NoPages:
        left = 0

        def __call__(self, url):
            raise AssertionError("not opened")

    d.render = NoPages()
    result = job_links.run(d, BOARDS)
    assert result.not_yet == 0


def test_a_reposted_job_is_deleted_from_the_list():
    """4 Nov, your decision: "a wrong or fake one: just delete it". A job you approved or
    built a resume for stays."""
    d = deps([row(company="Jobgether"), row(company="Jobgether", Status="Resume built"),
              row()], llm=None)
    result = job_links.run(d, BOARDS)
    assert "p0" not in d.repo.rows and "p1" in d.repo.rows and "p2" in d.repo.rows
    assert result.deleted == ["Jobgether, DevOps Engineer"]
    summary = SweepSummary(reposted=result.deleted)
    assert ("Deleted from Notion, posted by a site that reposts other companies' jobs (not "
            "the employer): 1") in summary.friendly_text()


def test_a_made_up_workday_host_is_repaired_too():
    """4 Nov: the search gave nn.workdayjobs.com (no such server) for NN Group; the real
    site is nn.wd3.myworkdayjobs.com."""
    post, _posted = workday_api("nn", "wd3", "nn_careers", [])
    d = deps([row(company="NN Group")])
    d.post = post
    made_up = "https://nn.workdayjobs.com/nn_careers/job/The-Hague/Medior-Engineer_REQALL-7901"
    assert job_links.workday_board(d, made_up).host == "nn.wd3.myworkdayjobs.com"


class RefusingSite(Search):
    """The company's own site refuses the bot; the job system search answers `board`."""

    def __init__(self, board, website="https://www.dunedata.com/careers"):
        super().__init__()
        self.board, self.website = board, website

    def complete_json_with_search(self, stage, system, user, **kwargs):
        self.calls.append((system, user))
        if system == job_links.SITE_SYSTEM:
            return {"website": "", "careers": self.website, "board": ""}, []
        if system == job_links.BOARD_SYSTEM:
            return {"board": self.board}, ["https://www.dunedata.com/careers/search"]
        return {"url": ""}, []


class Refuses(Pages):
    def __call__(self, url):
        self.opened.append(url)
        if "dunedata.com" in url:
            raise http.HttpError("GET failed: HTTP 403", status=403)
        return super().__call__(url)


def test_a_site_that_refuses_the_bot_gets_its_job_system_searched():
    """4 Nov: Serco's and PwC's own sites answered HTTP 403; the job system they post on
    is meant to be read by job sites."""
    post, posted = workday_api("dunedata", "wd3", "Careers",
                               [("DevOps Engineer", "Amsterdam, Netherlands", "DevOps_R1")])
    real = "https://dunedata.wd3.myworkdayjobs.com/en-US/Careers/job/DevOps_R1"

    def get(url, params):
        if url.endswith("/DevOps_R1"):
            return {"jobPostingInfo": {"title": "DevOps Engineer", "externalUrl": real,
                                       "location": "Amsterdam, Netherlands",
                                       "jobDescription": f"<p>{TEXT}</p>"}}
        raise http.HttpError("GET failed: HTTP 404", status=404)

    search = RefusingSite("https://dunedata.myworkdayjobs.com/Careers")
    d = deps([row()], get=get, llm=search)
    d.post, d.page = post, Refuses({})
    result = job_links.run(d, BOARDS)
    assert [s for s, _u in search.calls].count(job_links.BOARD_SYSTEM) == 1
    assert d.repo.rows["p0"]["URL"] == real
    assert result.labels == ["Dune Data, DevOps Engineer (company job board)"]


def test_the_job_system_search_takes_only_a_job_systems_link():
    search = RefusingSite("https://www.dunedata.com/jobs")  # the refusing site again
    d = deps([row()], llm=search)
    d.page = Refuses({})
    job_links.run(d, BOARDS)
    assert d.repo.rows["p0"]["URL"] == LINKEDIN  # nothing found, nothing made up
    assert [s for s, _u in search.calls].count(job_links.BOARD_SYSTEM) == 1


class GoogleCopy(Search):
    """The AI web search: nothing for the careers page and the job, then Google's copy."""

    def __init__(self, reply, sources):
        super().__init__()
        self.reply, self.copy_sources = reply, sources

    def complete_json_with_search(self, stage, system, user, **kwargs):
        if system == job_links.GOOGLE_SYSTEM:
            self.google.append(user)
            return self.reply, self.copy_sources
        return super().complete_json_with_search(stage, system, user, **kwargs)


ADVERT = ("DevOps Engineer in Amsterdam. You build and run our Kubernetes platform, write "
          "Terraform and keep the CI/CD pipelines green. " + TEXT)
OWN = "https://www.dunedata.com/careers/devops-engineer-amsterdam"


def test_a_site_that_could_not_be_checked_is_read_from_googles_copy():
    """4 Nov, your decision: Serco's site refuses the bot; Google has its page."""
    search = GoogleCopy({"found": True, "url": OWN, "description": ADVERT},
                        ["https://www.linkedin.com/jobs/view/1", OWN])
    d = deps([row()], llm=search, careers={"dune data": CAREERS})
    d.page = Refuses({})
    result = job_links.run(d, BOARDS)
    assert len(search.google) == 1
    assert d.repo.rows["p0"]["URL"] == OWN
    assert result.labels == [f"Dune Data, DevOps Engineer ({job_links.GOOGLE_HOW})"]
    body = "\n".join(d.repo.rows["p0"]["body"])
    assert "Google's copy of the company page" in body and "Kubernetes platform" in body
    assert d.page.opened == [CAREERS]  # the refusing site: never opened again


@pytest.mark.parametrize(("reply", "sources"), [
    ({"found": True, "url": "https://www.dunedata.com/careers", "description": ADVERT},
     ["https://www.indeed.com/viewjob?jk=1"]),  # no own source, the address names no job
    ({"found": True, "url": "https://www.jobbird.com/devops-engineer-amsterdam",
      "description": ADVERT},
     ["https://www.indeed.com/viewjob?jk=1"]),  # no own source, the answer on another site
    ({"found": True, "url": OWN, "description": "DevOps Engineer. Apply now."}, [OWN]),
    ({"found": True, "url": OWN, "description": "Data Analyst in Amsterdam. " + TEXT}, [OWN]),
    ({"found": False}, [OWN]),
])
def test_googles_copy_is_used_only_when_it_is_this_job_on_the_company_site(reply, sources):
    d = deps([row()], llm=GoogleCopy(reply, sources), careers={"dune data": CAREERS})
    d.page = Refuses({})
    job_links.run(d, BOARDS)
    assert d.repo.rows["p0"]["URL"] == LINKEDIN


def test_googles_copy_takes_its_answer_on_the_company_site_when_it_names_the_job():
    """4 Nov: Serco, Medtronic and HSBC came back with the job's own address as the answer
    but only LinkedIn and Indeed as sources."""
    search = GoogleCopy({"found": True, "url": OWN, "description": ADVERT},
                        ["https://www.linkedin.com/jobs/view/1",
                         "https://www.indeed.com/viewjob?jk=1"])
    d = deps([row()], llm=search, careers={"dune data": CAREERS})
    d.page = Refuses({})
    result = job_links.run(d, BOARDS)
    assert d.repo.rows["p0"]["URL"] == OWN
    assert result.labels == [f"Dune Data, DevOps Engineer ({job_links.GOOGLE_HOW})"]


def test_an_address_names_the_job_only_with_every_title_word():
    assert job_links._url_names_role(OWN, "DevOps Engineer")
    assert job_links._url_names_role("https://x.com/jobs/Senior_DevOps_Engineer%20NL",
                                     "DevOps Engineer")
    assert not job_links._url_names_role("https://x.com/jobs/devops-lead", "DevOps Engineer")
    assert not job_links._url_names_role("https://x.com/jobs/1", "DevOps Engineer")


@pytest.mark.parametrize("answer", [
    "https://www.dunedata.com/eu/careers/find-jobs/job-details?jobId=313360",  # Serco
    "https://boards.greenhouse.io/dunedata/jobs/7478061002",  # Sony
])
def test_googles_answer_with_a_job_number_on_the_company_site_counts(answer):
    """4 Nov: Serco's and Sony's answers named the job by its number, not its title."""
    search = GoogleCopy({"found": True, "url": answer, "description": ADVERT}, [])
    d = deps([row()], llm=search, careers={"dune data": CAREERS})
    d.page = Refuses({})
    job_links.run(d, BOARDS)
    assert d.repo.rows["p0"]["URL"] == answer


def test_googles_redirect_link_as_its_answer_is_followed(monkeypatch):
    """4 Nov: ChipSoft's answer was Google's own redirect link."""
    from jobengine import http
    from jobengine.llm import GROUNDING_REDIRECT

    monkeypatch.setattr(http, "redirect_location", lambda url, s=None: OWN)
    search = GoogleCopy({"found": True, "url": GROUNDING_REDIRECT + "AUZIYQ", "description":
                         ADVERT}, [])
    d = deps([row()], llm=search, careers={"dune data": CAREERS})
    d.page = Refuses({})
    job_links.run(d, BOARDS)
    assert d.repo.rows["p0"]["URL"] == OWN


def test_googles_copy_when_the_job_list_needed_the_browser_and_none_was_left(monkeypatch):
    """4 Nov: TNO, CGI and SURF: their job lists need the browser, this job's browser pages
    were used up, so the list was never really read: no ghost, and Google's copy is asked."""
    monkeypatch.setattr(job_links, "MAX_RENDERS_PER_JOB", 0)
    thin = ("<html><body><a href='/careers/jobs'>Jobs</a>"
            + "".join(f"<a href='/p{i}'>Senior Engineer {i}</a>" for i in range(5))
            + "</body></html>")
    search = GoogleCopy({"found": True, "url": OWN, "description": ADVERT}, [OWN])
    d = deps([row()], pages={CAREERS: thin}, llm=search, careers={"dune data": CAREERS})
    browser = Pages({})
    browser.left = 40
    d.render = browser
    job_links.run(d, BOARDS)
    assert len(search.google) == 1 and d.repo.rows["p0"]["URL"] == OWN
    assert browser.opened == []


def test_job_addresses_in_other_languages_and_two_letter_names():
    """4 Nov: ASTEK's "/devops-inzynier-km-2/", NN Group's nn-careers.com."""
    assert job_links._one_job_address("https://astek.pl/oferty-pracy/devops-inzynier-km-2/",
                               "DevOps Engineer")
    assert job_links._company_host("https://www.nn-careers.com/en/vacancies/1", "NN Group")
    assert not job_links._company_host("https://www.nnn.com/jobs/1", "NN Group")
    assert not job_links._one_job_address("https://www.dunedata.com/careers", "DevOps Engineer")


def test_googles_careers_site_short_job_system_numbers_and_countries():
    """4 Nov: Google's own careers page, CGI's Njoyn link, a row whose company was "Poland"."""
    google = ("https://www.google.com/about/careers/applications/jobs/results/"
              "140683074818966214-cloud-engineer-data-and-ai-google-cloud")
    assert job_links._company_host(google, "Google")
    assert not job_links._company_host(google, "Dune Data")
    assert not job_links.employer_page("https://www.google.com/search?q=jobs", "Google")
    njoyn = "https://nl.njoyn.com/CGI/urlredirect.jso?J1026-2636&lang=nl"
    assert job_links._one_job_address(njoyn, "DevOps Engineer - Digitale Overheid")
    assert not job_links._one_job_address("https://www.dunedata.com/jobs?page=2026",
                                          "DevOps Engineer")
    assert not job_links.real_company("Poland")


def test_privacy_and_cookie_links_are_not_careers_pages():
    """4 Nov: on careers.kingfisher.com its privacy policy was opened in the browser."""
    html = ("<a href='https://careers.dunedata.com/privacy-policy/'>Privacy</a>"
            "<a href='https://careers.dunedata.com/jobs/'>Jobs</a>")
    assert job_links.careers_links("https://careers.dunedata.com/", html) == [
        "https://careers.dunedata.com/jobs/"]


def test_googles_copy_never_brings_back_a_ghost_job():
    """Its job list was read and the job is not on it: Google is not asked."""
    listed = careers_page(("/careers/java-developer", "Java Developer"),
                          ("/careers/data-engineer", "Data Engineer"),
                          ("/careers/frontend-engineer", "Frontend Engineer"))
    search = GoogleCopy({"found": True, "url": OWN, "description": ADVERT}, [OWN])
    d = deps([row()], pages={CAREERS: listed}, llm=search, careers={"dune data": CAREERS})
    job_links.run(d, BOARDS)
    assert search.google == [] and d.repo.rows["p0"]["URL"] == LINKEDIN


def test_a_jobs_box_from_another_site_is_read_with_the_page():
    """4 Nov: a careers page that shows its jobs in a box from Recruitee, Homerun or
    Personio (an iframe): the box's HTML comes after the page's."""
    from jobengine.sweep import render

    class Frame:
        def __init__(self, url, html, fails=False):
            self.url, self.html, self.fails = url, html, fails

        def content(self):
            if self.fails:
                raise RuntimeError("frame was detached")
            return self.html

    class Page:
        frames = [Frame("https://www.dunedata.com/careers", "<main/>"),
                  Frame("https://dunedata.recruitee.com/embed", "<a href='/o/devops'>DevOps "
                        "Engineer</a>"),
                  Frame("https://ads.example.com", "", fails=True)]

    out = render._frames(Page())
    assert "frame https://dunedata.recruitee.com/embed" in out
    # its links are made whole with the box's own site, not the careers page's
    assert "<a href='https://dunedata.recruitee.com/o/devops'>DevOps Engineer</a>" in out
    assert "<main/>" not in out


def test_a_short_company_name_is_seen_in_its_web_address():
    """4 Nov: P&G's careers site is pgcareers.com; "p g" has no word of 3 letters, so
    Google's copy of its page was refused as "no source on the company's own site"."""
    assert job_links.name_in_host("www.pgcareers.com", "p g")
    assert job_links.name_in_host("careers.serco.com", "serco")
    assert not job_links.name_in_host("www.example.com", "p g")
    assert not job_links.name_in_host("jobs.ab.com", "a")  # never a one-letter name
    assert job_links.names_company("https://www.pgcareers.com/sre", "", "P&G")
    assert job_links._company_host("https://www.pgcareers.com/global/en/job/R1", "P&G")


def test_a_better_lookup_tries_the_jobs_not_found_before_once():
    """4 Nov: after the fixes the jobs looked up in the morning waited 3 days; with a new
    lookup version they are looked up again once (the ghost counts stay)."""
    state = FakeBotState()
    state.set(job_links.KEY, {"tried": {"p0": TODAY.isoformat()},
                              "sites": {"dune data": {"url": "", "date": TODAY.isoformat()}},
                              job_links.MISSES: {"p9": [TODAY.isoformat()]}})
    search = Search()
    d = deps([row()], llm=search, state=state)
    result = job_links.run(d, BOARDS)
    assert result.tried == 1 and search.calls  # looked up again, its careers page searched
    saved = state.get(job_links.KEY)
    assert saved["version"] == job_links.LOOKUP_VERSION
    assert saved[job_links.MISSES] == {"p9": [TODAY.isoformat()]}
    again = job_links.run(deps([row()], llm=Search(), state=state), BOARDS)
    assert again.tried == 0  # once only: now it waits its 3 days as before


def test_a_close_title_in_the_same_place_is_the_same_job():
    """4 Nov: JustJoin's "DevOps Engineer" at Motorola Solutions was skipped as a ghost job,
    while Motorola's own board had "Software DevOps Engineer" in Krakow."""
    assert job_links.close_title("Software DevOps Engineer", "DevOps Engineer", "Krakow",
                                 "Poland")
    assert not job_links.close_title("Senior DevOps Engineer", "DevOps Engineer")
    assert not job_links.close_title("DevOps Engineer Intern Program Lead", "DevOps Engineer")
    assert not job_links.close_title("Data Engineer", "AWS Databricks Platform Engineer")
    url = "https://boards.greenhouse.io/dunedata/jobs/9"
    get = board_api([("Software DevOps Engineer", "Amsterdam", url),
                     ("DevOps Engineer", "Berlin, Germany", "https://boards.greenhouse.io/x/1")])
    d = deps([row()], pages={url: job_page("Software DevOps Engineer")}, get=get)
    assert job_links.run(d, BOARDS).labels == ["Dune Data, DevOps Engineer (company job board)"]
    assert d.repo.rows["p0"]["URL"] == url


def test_two_close_titles_are_not_a_guess_and_not_a_ghost():
    get = board_api([("Software DevOps Engineer", "Amsterdam", "https://boards.greenhouse.io/d/1"),
                     ("Cloud DevOps Engineer", "Amsterdam", "https://boards.greenhouse.io/d/2"),
                     ("Data Analyst", "Amsterdam", "https://boards.greenhouse.io/d/3")])
    state = FakeBotState()
    for _ in range(2):  # two checks
        d = deps([row()], get=get, state=state)
        state.set(job_links.KEY, {**(state.get(job_links.KEY) or {}), "tried": {}})
        result = job_links.run(d, BOARDS)
    assert d.repo.rows["p0"]["URL"] == LINKEDIN and result.ghosts == []


def test_link_text_and_city_lists_are_not_part_of_the_title():
    """4 Nov: CIMSOLUTIONS lists "Bekijk vacature voor DevOps Engineer"; Grid Dynamics
    "Site Reliability Engineer Wroclaw, Krakow, Gdansk, Warsaw, Poland"."""
    assert job_links.same_title("Bekijk vacature voor DevOps Engineer", "DevOps Engineer")
    assert job_links.same_title("View vacancy for Platform Engineer", "Platform Engineer")
    assert job_links.same_title("Site Reliability Engineer Wroclaw, Krakow, Gdansk, Warsaw, "
                                "Poland", "Site Reliability Engineer", "Wrocław", "Poland")
    assert not job_links.same_title("Senior Site Reliability Engineer Krakow, Poland",
                                    "Site Reliability Engineer", "Kraków", "Poland")


def test_a_page_is_opened_in_the_browser_once_per_fetch():
    """4 Nov: CGI's, Apple's and Prism's pages were opened again for each of their jobs."""
    from jobengine.sweep import render

    class Thread:
        def __init__(self):
            self.opened = []

        def submit(self, fn, url):
            self.opened.append(url)

            class Done:
                def result(self):
                    if "fails" in url:
                        raise http.HttpError("page not rendered: timeout")
                    return url, "<html/>"
            return Done()

    browser = render.Renderer(load_settings("local", {}), left=5)
    browser._thread = Thread()
    for _ in range(3):
        assert browser("https://www.dunedata.com/careers") == (
            "https://www.dunedata.com/careers", "<html/>")
        with pytest.raises(http.HttpError):
            browser("https://www.dunedata.com/fails")
    assert browser._thread.opened == ["https://www.dunedata.com/careers",
                                      "https://www.dunedata.com/fails"]
    assert browser.left == 3


def test_googles_copy_can_use_a_stronger_gemini():
    search = GoogleCopy({"found": False}, [])
    seen = {}

    def spy(stage, system, user, **kwargs):
        seen.update(kwargs)
        return search.reply, []

    search.complete_json_with_search = spy
    d = deps([row()], llm=search, careers={"dune data": CAREERS})
    d.page = Refuses({})
    d.s.sweep["job_links"] = {**(d.s.sweep.get("job_links") or {}),
                              "copy_model": "gemini-3.8-flash"}
    job_links.run(d, BOARDS)
    assert seen.get("gemini_model") == "gemini-3.8-flash"


def test_careers_addresses_with_initials_or_a_careers_word():
    """4 Nov: P&G (Procter & Gamble) is pgcareers.com, a.s.r. werkenbijasr.nl, UNIQA's
    adverts are on erecruiter.pl."""
    assert job_links._company_host("https://www.pgcareers.com/job/R1", "Procter & Gamble")
    assert job_links._company_host("https://www.werkenbijasr.nl/vacatures/1", "a.s.r.")
    assert job_links._company_host("https://www.werkenbijbng.nl/vacature/2", "BNG")
    assert job_links._company_host("https://system.erecruiter.pl/FormTemplates/1", "UNIQA")
    assert not job_links._company_host("https://www.werkenbijdefensie.nl/1", "a.s.r.")


def test_a_close_title_with_its_place_after_it():
    """4 Nov: Motorola's "Software DevOps Engineer (Krakow, Poland)" for JustJoin's
    "DevOps Engineer" (the row had no city)."""
    assert job_links.close_title("Software DevOps Engineer (Krakow, Poland)", "DevOps Engineer",
                                 None, "Poland")
    assert not job_links.close_title("Intern DevOps Engineer (Krakow, Poland)",
                                     "DevOps Engineer", None, "Poland")


def test_googles_copy_never_falls_back_to_claude():
    search = GoogleCopy({"found": False}, [])
    seen = {}

    def spy(stage, system, user, **kwargs):
        seen.update(kwargs)
        return search.reply, []

    search.complete_json_with_search = spy
    d = deps([row()], llm=search, careers={"dune data": CAREERS})
    d.page = Refuses({})
    job_links.run(d, BOARDS)
    assert seen.get("fallback") is False
