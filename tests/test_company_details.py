"""Target Companies details (5 Nov, your ask): the companies the bot added get their empty
columns filled by /fetch. Made-up companies and pages."""

from datetime import date, timedelta

import pytest
from test_telegram_bot import settings

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.notion_repo import FakeCompanyAdder
from jobengine.reference import Company
from jobengine.safety import SafetyError
from jobengine.sweep import company_details, job_links
from jobengine.sweep.models import SweepSummary

TODAY = date(2026, 11, 5)
CAREERS = "https://careers.dunedata.com/jobs"
PAGE = ("<html><body><h1>Careers at Dune Data</h1><a href='/jobs/1'>DevOps Engineer</a>"
        "</body></html>")
ANSWER = {"website": "https://www.dunedata.com", "careers": CAREERS, "board": "",
          "city": "Amsterdam", "tier": "Tier 2 - Tech/Cloud"}


class Search:
    def __init__(self, raw=None):
        self.raw = dict(ANSWER if raw is None else raw)
        self.calls = []

    def complete_json_with_search(self, stage, system, user, **kwargs):
        assert system == company_details.DETAILS_SYSTEM
        self.calls.append(user)
        return self.raw, []


class Register:
    def __init__(self, names):
        self.names = names

    def match(self, name):
        return name in self.names


def pages(url):
    if url == CAREERS:
        return url, PAGE
    raise http.HttpError("GET failed: HTTP 404", status=404)


def deps(llm, state=None):
    return job_links.Deps(s=settings(), repo=None, page=pages, llm=llm,
                          state=state if state is not None else FakeBotState(), today=TODAY)


def company(name="Dune Data", region="Netherlands", notes="Added by /newcompanies on 2026-11-05",
            row_id="tc-1", **extra):
    return Company(row_id=row_id, name=name, region=region, notes=notes,
                   ats_platform="Unknown", **extra)


def test_the_empty_columns_of_a_company_the_bot_added_are_filled():
    adder, llm = FakeCompanyAdder(), Search()
    result = company_details.fill(deps(llm), [company()], adder.fill, Register(["Dune Data"]))
    assert adder.rows["tc-1"] == {
        "Careers URL": CAREERS, "ATS platform": "Custom", "Domain": "dunedata.com",
        "Hub / City": "Amsterdam", "Tier": "Tier 2 - Tech/Cloud", "IND sponsor": "Verified"}
    assert result.filled == ["Dune Data (Careers URL, ATS platform, Domain, Hub / City, Tier, "
                             "IND sponsor)"]
    assert result.searches == 1 and llm.calls == ["Company: Dune Data\nCountry: Netherlands"]


def test_a_row_you_added_by_hand_and_a_filled_column_are_never_touched():
    adder, llm = FakeCompanyAdder(), Search()
    mine = company(notes="My own note")
    company_details.fill(deps(llm), [mine], adder.fill)
    assert adder.rows == {} and llm.calls == []
    # only the IND column empty: no web search, "N/A" outside the Netherlands
    done = company(region="Poland", careers_url=CAREERS, domain="dunedata.com",
                   hub_city="Krakow", tier="Tier 4 - Consulting/GBS")
    company_details.fill(deps(llm), [done], adder.fill)
    assert adder.rows["tc-1"] == {"IND sponsor": "N/A"} and llm.calls == []


def test_an_answer_that_is_not_the_company_or_a_made_up_tier_is_left_out():
    raw = {"website": "https://www.linkedin.com/company/dunedata", "careers":
           "https://www.indeed.com/cmp/dune-data/jobs", "board": "", "city": "",
           "tier": "Tier 9 - Space"}
    adder = FakeCompanyAdder()
    company_details.fill(deps(Search(raw)), [company(region="Ireland")], adder.fill)
    assert adder.rows["tc-1"] == {"IND sponsor": "N/A"}


def test_a_company_is_looked_up_again_only_after_some_days_and_a_few_a_run():
    state, adder = FakeBotState(), FakeCompanyAdder()
    llm = Search({"website": "", "careers": "", "board": "", "city": "", "tier": ""})
    rows = [company(name=f"Dune Data {i}", row_id=f"tc-{i}") for i in range(3)]
    company_details.fill(deps(llm, state), rows, adder.fill, max_per_run=2)
    assert len(llm.calls) == 2
    company_details.fill(deps(llm, state), rows, adder.fill, max_per_run=2)
    assert len(llm.calls) == 3  # the third one now; the first two wait RETRY_DAYS
    later = deps(llm, state)
    later.today = TODAY + timedelta(days=company_details.RETRY_DAYS + 1)
    company_details.fill(later, rows, adder.fill, max_per_run=5)
    assert len(llm.calls) == 6  # all three again: each still has empty columns


def test_only_the_fill_columns_may_be_written():
    with pytest.raises(SafetyError):
        FakeCompanyAdder().fill("tc-1", {"Company": "Other name"})
    with pytest.raises(SafetyError):
        FakeCompanyAdder().fill("tc-1", {"Active": False})


def test_the_summary_names_the_companies_filled():
    text = SweepSummary(details_filled=["Dune Data (Careers URL, Tier)"]).friendly_text()
    assert ("Target Companies details filled (empty columns of the companies the bot added): 1"
            in text)
    assert "- Dune Data (Careers URL, Tier)" in text


def test_a_fetch_runs_the_details_pass():
    from jobengine.sweep.fakes import FAKE_TODAY
    from jobengine.sweep.runner import fake_deps, run_sweep

    s = settings()
    calls = []
    d = fake_deps(s)
    d.details = lambda *args: (calls.append(args), company_details.Result(
        filled=["Dune Data (Tier)"]))[1]
    summary = run_sweep(s, d, FAKE_TODAY, state=FakeBotState())
    assert len(calls) == 1 and summary.details_filled == ["Dune Data (Tier)"]
