"""LOG-08 (24 Nov): Workday refused this PC (HTTP 429) on every /fetch. Workday is now asked
less: 3 searches per site, only some sites per /fetch (the ones read longest ago, each at
most once a day), and a refusal is remembered in bot_state for the next /fetch."""

from datetime import UTC, date, datetime, timedelta

import pytest

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.settings import load_settings
from jobengine.sweep.models import SweepSummary, TargetCompany
from jobengine.sweep.sources import ats

S = load_settings("local", {})
TODAY = date(2026, 11, 24)
NOW = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)
COMPANIES = [TargetCompany(f"Co{i}", f"https://co{i}.wd3.myworkdayjobs.com/External",
                           "Workday", True) for i in range(14)]


def keep_all(title, location):
    return True


def no_page(url):
    pytest.fail(f"no careers page should be read: {url}")


class Workday:
    """Fake Workday sites: one job each, or HTTP 429 from the `refuse_from`-th search on."""

    def __init__(self, refuse_from=None):
        self.searches = []
        self.refuse_from = refuse_from

    def post(self, url, body):
        tenant = url.split("//")[1].split(".")[0]
        self.searches.append((tenant, body["searchText"]))
        if self.refuse_from is not None and len(self.searches) >= self.refuse_from:
            http.pause_workday(http.SLOW_CLOSE)  # what http.py does after two refusals
            raise http.HttpError("POST failed: HTTP 429", 429)
        if not body["searchText"]:
            return {"total": 0, "jobPostings": []}
        return {"total": 1, "jobPostings": [
            {"title": "DevOps Engineer", "externalPath": f"/job/{tenant}-1",
             "locationsText": "Krakow, Poland", "postedOn": "Posted Today"}]}

    def get(self, url, params):
        return {}

    def tenants(self):
        return sorted({tenant for tenant, _ in self.searches}, key=lambda t: int(t[2:]))


def sweep(site, state, now=NOW):
    return ats.fetch(S, COMPANIES, keep_all, get=site.get, post=site.post, page=no_page,
                     state=state, today=TODAY, now=now)


def test_ten_sites_a_fetch_with_three_searches_each():
    site, state = Workday(), FakeBotState()
    result = sweep(site, state)
    assert site.tenants() == [f"co{i}" for i in range(10)]
    assert {term for _, term in site.searches} == {"", "devops", "site reliability",
                                                   "cloud platform"}
    assert len(site.searches) == 10 * 4  # the whole list once, then 3 words
    assert result.workday_line == "Workday: 10 of 14 sites read; 4 in the next runs"
    assert not result.blocked


def test_the_next_fetch_reads_the_others_and_a_site_once_a_day():
    state = FakeBotState()
    sweep(Workday(), state)
    later = Workday()
    result = sweep(later, state, NOW + timedelta(hours=2))
    assert later.tenants() == ["co10", "co11", "co12", "co13"]
    assert result.workday_line == ("Workday: 4 of 14 sites read (10 read earlier today)")
    nothing = Workday()
    assert sweep(nothing, state, NOW + timedelta(hours=4)).workday_line == (
        "Workday: 0 of 14 sites read (14 read earlier today)")
    assert nothing.searches == []
    next_day = Workday()
    sweep(next_day, state, NOW + timedelta(hours=21))
    assert next_day.tenants() == [f"co{i}" for i in range(10)]  # read longest ago first


def test_a_refusal_stops_workday_and_is_remembered(monkeypatch):
    monkeypatch.setattr(ats, "run_all", lambda fn, items, workers: [fn(i) for i in items])
    state = FakeBotState()
    site = Workday(refuse_from=9)  # two sites answer, the third is refused
    result = sweep(site, state)
    assert site.tenants() == ["co0", "co1", "co2"]  # Workday is not asked after the refusal
    assert not result.blocked  # left for later, not "blocks the bot"
    line = result.workday_line
    assert line.startswith("Workday: 2 of 14 sites read; 12 in the next runs; Workday "
                           "refused this PC, asked again in about 6 h")
    saved = state.get(ats.WORKDAY_KEY)
    assert set(saved["read"]) == {"co0|co0.wd3.myworkdayjobs.com|external",
                                  "co1|co1.wd3.myworkdayjobs.com|external"}
    assert datetime.fromisoformat(saved["refused_until"]) > NOW + timedelta(hours=5)

    # The bot is restarted (a fresh gate): the next /fetch does not ask Workday at all.
    http.reset_slow_hosts()
    again = Workday()
    result = sweep(again, state, NOW + timedelta(hours=1))
    assert again.searches == []
    assert result.workday_line == ("Workday: not asked (it refused this PC earlier; asked "
                                   "again in about 5 h); its 14 sites wait")
    assert http.workday_pause_left() > 4 * 3600  # job pages wait too

    # After the rest time it is asked again, the sites not read yet first.
    http.reset_slow_hosts()
    rested = Workday()
    sweep(rested, state, NOW + timedelta(hours=7))
    assert rested.tenants()[0] == "co2"


def test_the_fetch_message_has_the_workday_line():
    summary = SweepSummary(workday_line="Workday: 10 of 14 sites read; 4 in the next runs")
    assert "\U0001F3E2 Workday: 10 of 14 sites read; 4 in the next runs" in summary.short_text()
    assert "\U0001F3E2 Workday: 10 of 14" in summary.friendly_text()
    assert "Workday" not in SweepSummary().short_text()


def test_workday_rests_six_hours_after_a_refusal():
    assert http.SLOW_CLOSE >= 6 * 3600
    cfg = S.sweep["ats"]
    assert cfg["workday_sites_per_run"] == 10
    assert len(cfg["workday_search_terms"]) == 3
