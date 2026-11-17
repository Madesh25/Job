"""Job boards learned from job links (13 Nov, your idea 1). Made-up companies."""

from datetime import date, timedelta

from test_discover import COMPANIES, SEEN, job

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.sweep import auto_boards

TODAY = date(2026, 11, 13)


def test_a_board_in_a_job_link_is_learned_once():
    state = FakeBotState()
    jobs = [*SEEN,
            job("Nova Systems", "SRE", board="IamExpat",
                url="https://jobs.lever.co/novasystems/abc"),  # second board: kept the first
            job("Hays Poland", "Cloud Engineer", url="https://jobs.ashbyhq.com/hays/1"),  # agency
            job("Old Co", "DevOps Engineer", url="https://jobs.ashbyhq.com/oldco/1"),  # turned down
            job("Acme Cloud", "Cloud Engineer", url="https://jobs.ashbyhq.com/acme/9"),  # target
            job("Pace Labs", "DevOps Engineer", url="https://apply.workable.com/pacelabs/j/1"),
            job("Shop Co", "DevOps Engineer", url="https://www.amazon.jobs/en/jobs/1")]  # not read
    new = auto_boards.from_jobs(state, jobs, COMPANIES, ConfigStore.fake(), ["old co"], TODAY)
    assert new == ["Nova Systems", "Pace Labs"]
    rows = auto_boards.companies(state, COMPANIES)
    assert [(c.name, c.careers_url, c.ats_platform) for c in rows] == [
        ("Nova Systems", "https://boards.greenhouse.io/novasystems/jobs/7", "Greenhouse"),
        ("Pace Labs", "https://apply.workable.com/pacelabs/j/1", "Custom")]
    assert all(c.active for c in rows)
    # Seen again later: not new, kept longer.
    again = auto_boards.from_jobs(state, jobs, COMPANIES, ConfigStore.fake(), ["old co"], TODAY)
    assert again == []
    assert auto_boards.board_for(state, "Nova Systems Sp. z o.o.").token == "novasystems"
    assert auto_boards.board_for(state, "Quiet Labs") is None


def test_turned_down_or_added_companies_are_not_read_and_old_ones_go():
    state = FakeBotState()
    jobs = [job("Pace Labs", "DevOps Engineer", url="https://apply.workable.com/pacelabs/j/1")]
    auto_boards.from_jobs(state, jobs, COMPANIES, ConfigStore.fake(), [], TODAY)
    assert auto_boards.companies(state, COMPANIES, skip=["pace labs"]) == []
    from jobengine.sweep.models import TargetCompany

    added = [*COMPANIES, TargetCompany("Pace Labs", "", "Unknown", True)]
    assert auto_boards.companies(state, added) == []
    later = TODAY + timedelta(days=61)
    other = [job("Kite Data", "SRE", url="https://jobs.lever.co/kitedata/1")]
    auto_boards.from_jobs(state, other, COMPANIES, ConfigStore.fake(), [], later)
    assert [c.name for c in auto_boards.companies(state, COMPANIES)] == ["Kite Data"]


def test_a_fetch_reads_the_learned_boards_and_says_so():
    from test_telegram_bot import settings

    from jobengine.sweep.fakes import FAKE_TODAY
    from jobengine.sweep.runner import fake_deps, run_sweep

    s = settings()
    state = FakeBotState()
    state.set(auto_boards.KEY, {"items": {"kite data": {
        "name": "Kite Data", "url": "https://jobs.lever.co/kitedata/1", "ats": "lever",
        "last": FAKE_TODAY.isoformat()}}})
    deps = fake_deps(s)
    asked = []
    plain = deps.ats

    def ats(companies, keep, state=None):
        asked.extend(c.name for c in companies)
        return plain(companies, keep, state)

    deps.ats = ats
    summary = run_sweep(s, deps, FAKE_TODAY, state=state)
    assert "Kite Data" in asked
    assert summary.learned_boards == 1
    assert "Company job boards learned from job links, read too: 1" in summary.friendly_text()
