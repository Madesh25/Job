"""Company discovery and the "company site missed it" check (C1, 26 Oct). Made-up companies."""

from datetime import date

from test_telegram_bot import Clock, settings

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.sweep import discover
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.sweep.models import Job, SweepSummary, TargetCompany

TODAY = date(2026, 10, 26)


def job(company, role, source="gmail", board="LinkedIn", url="https://www.linkedin.com/jobs/1",
        country="Poland", city="Kraków"):
    return Job(source=source, board=board, company=company, role=role, city=city,
               country=country, url=url, posted_date=None, salary=None, seniority="",
               years_required=None, dedupe_key=f"{company}|{role}", posting_ref=f"{source}:1",
               description=None, description_is_snippet=False)


COMPANIES = [TargetCompany("Acme Cloud", "https://boards.greenhouse.io/acme", "Greenhouse", True),
             TargetCompany("Beta Soft (IE)", "", "Unknown", True),
             TargetCompany("Gone Corp", "", "Unknown", False)]
SEEN = [
    job("Acme Cloud", "DevOps Engineer", source="ats", board="Company site",
        url="https://boards.greenhouse.io/acme/jobs/1"),
    job("Acme Cloud Sp. z o.o.", "DevOps Engineer"),  # on the board too: not missed
    job("Acme Cloud", "Site Reliability Engineer", board="Adzuna",
        url="https://www.adzuna.pl/details/9"),  # the board did not list it
    job("Beta Soft", "Platform Engineer"),  # its board was not read this time
    job("Gone Corp", "DevOps Engineer"),  # unticked in Target Companies: never suggested
    job("Nova Systems", "DevOps Engineer", board="Jooble",
        url="https://boards.greenhouse.io/novasystems/jobs/7"),
    job("Nova Systems", "Cloud Engineer", country="Netherlands", city="Amsterdam"),
    job("Hays Poland", "DevOps Engineer"),  # an agency
    job("Old Co", "DevOps Engineer"),  # you said "Not interested" before
    job("Quiet Labs", "SRE", url="https://www.linkedin.com/jobs/2"),
]


def test_new_companies_and_jobs_the_company_board_missed():
    found = discover.find(SEEN, COMPANIES, ["Acme Cloud"], ConfigStore.fake(),
                          skip=["old co"], today=TODAY)
    assert found.missed == ["Acme Cloud, Site Reliability Engineer (Adzuna)"]
    assert sorted(found.new) == ["nova systems", "quiet labs"]
    nova = found.new["nova systems"]
    assert nova["roles"] == ["DevOps Engineer", "Cloud Engineer"]
    assert nova["countries"] == ["Poland", "Netherlands"]
    assert nova["boards"] == ["Jooble", "LinkedIn"]


def test_the_site_comes_from_the_job_links_never_from_a_job_board():
    assert discover.site_of(["https://www.linkedin.com/jobs/1",
                             "https://boards.greenhouse.io/novasystems/jobs/7"]) == (
        "https://boards.greenhouse.io/novasystems", "Greenhouse")
    assert discover.site_of(["https://www.adzuna.pl/details/9",
                             "https://careers.quietlabs.example/jobs/3"]) == (
        "https://careers.quietlabs.example/", "Unknown")
    assert discover.site_of(["https://www.linkedin.com/jobs/2",
                             "https://pl.jooble.org/desc/5"]) == ("", "Unknown")


def test_suggestions_are_kept_and_the_readable_board_comes_first():
    state = FakeBotState()
    found = discover.find(SEEN, COMPANIES, ["Acme Cloud"], ConfigStore.fake(), today=TODAY)
    assert discover.record(state, found, TODAY) == 3  # Nova, Quiet Labs, Old Co
    assert discover.record(state, found, TODAY) == 0  # seen again: nothing new
    names = [item["name"] for _, item in discover.suggestions(state, COMPANIES)]
    assert names[0] == "Nova Systems"  # its Greenhouse board can be read
    discover.drop(state, "old co", never_again=True)
    assert discover.skipped(state) == ["old co"]
    assert "Old Co" not in [i["name"] for _, i in discover.suggestions(state, COMPANIES)]
    later = discover.find(SEEN[-1:], COMPANIES, [], ConfigStore.fake(), today=date(2026, 12, 1))
    discover.record(state, later, date(2026, 12, 1))  # a month later: old ones are dropped
    assert [i["name"] for _, i in discover.suggestions(state, COMPANIES)] == ["Quiet Labs"]


def test_the_fetch_summary_names_both():
    text = SweepSummary(site_missed=["Acme Cloud, SRE (Adzuna)"], new_companies=2).friendly_text()
    assert ("Jobs another source found that the company's own job board did not list: 1\n"
            "- Acme Cloud, SRE (Adzuna)") in text
    assert "New companies hiring for your roles, not in Target Companies: 2. /newcompanies" in text


def test_newcompanies_adds_or_turns_down_one_at_a_time():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    assert d.newcompanies_command()[0].text.startswith("No new companies to suggest.")
    found = discover.find(SEEN, COMPANIES, [], ConfigStore.fake(), skip=["old co"],
                          today=TODAY)
    discover.record(d.state, found, TODAY)
    [card] = d.newcompanies_command()
    assert card.text.splitlines()[:4] == [
        "\U0001F50E New company 1 of 2: Nova Systems",
        "Jobs seen (Poland, Netherlands): DevOps Engineer; Cloud Engineer",
        "Found through: Jooble, LinkedIn",
        "Job board: Greenhouse, https://boards.greenhouse.io/novasystems (every /fetch can "
        "read it)"]
    add, no = card.buttons
    assert add[0] == "Add to Target Companies" and no[0] == "Not interested"
    added, nxt = d.tap(add[1])
    assert added.text.startswith("Added Nova Systems to Target Companies with its Greenhouse "
                                 "job board: https://boards.greenhouse.io/novasystems")
    [row] = d.companies.rows.values()
    assert row["Careers URL"] == "https://boards.greenhouse.io/novasystems"
    assert row["Region"] == "Poland" and row["Notes"].startswith("Added by /newcompanies on ")
    assert nxt.text.startswith("\U0001F50E New company 1 of 1: Quiet Labs")
    assert "Site: not known yet (the next /fetch looks for its job board by the name)" in nxt.text
    turned, last = d.tap(nxt.buttons[1][1])
    assert turned.text == "OK: Quiet Labs is not suggested again."
    assert last.text.startswith("No new companies to suggest.")
    assert "quiet labs" in discover.skipped(d.state)
    assert d.tap(nxt.buttons[0][1])[0].text == "That suggestion is no longer open."


def test_a_company_added_without_a_link_gets_no_careers_url():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    discover.record(d.state, discover.find(SEEN[-1:], COMPANIES, [], ConfigStore.fake(),
                                           today=TODAY), TODAY)
    [card] = d.newcompanies_command()
    added = d.tap(card.buttons[0][1])[0]
    assert added.text.startswith("Added Quiet Labs to Target Companies with no link yet")
    [row] = d.companies.rows.values()
    assert "Careers URL" not in row and row["ATS platform"] == "Unknown"


def test_a_fetch_keeps_the_suggestions_and_names_the_misses():
    from jobengine.sweep.runner import fake_deps, run_sweep

    s = settings()
    state = FakeBotState()
    summary = run_sweep(s, fake_deps(s), FAKE_TODAY, state=state)
    names = [item["name"] for _, item in discover.suggestions(state, [])]
    assert summary.new_companies == len(names) > 0
    assert "Odra Systems" in names  # "S.A." is not part of the name
    assert summary.site_missed == ["Baltic Bank, Junior DevOps Engineer (k/m) (Adzuna)"]
    assert "/newcompanies to add them" in summary.friendly_text()
