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
    """Stands in for the AI web search: the job search answers `url`, the careers page
    search answers `careers`."""

    def __init__(self, url="", sources=(), careers=""):
        self.answer = {"url": url}
        self.sources = list(sources)
        self.careers = careers
        self.calls = []

    def complete_json_with_search(self, stage, system, user, **kwargs):
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
    assert len(search.calls) == 1 and result.searches == 0


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


def test_a_long_page_is_not_opened_in_the_browser():
    """30 Oct: CGI's long pages used 5 of the 15 browser pages and found nothing."""
    d = deps([row()], pages={CAREERS: long_page()}, careers={"dune data": CAREERS})
    browser = Pages({})
    d.render = browser
    job_links.run(d, BOARDS)
    assert browser.opened == []


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
