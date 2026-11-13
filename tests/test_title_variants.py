"""Fetch idea 6 (13 Nov): one job under two titles ("DevSecOps" and "DevSecOps Engineer",
Lepaya, 10 Nov) is one row."""

from test_telegram_bot import settings

from jobengine.bot_state import FakeBotState
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.sweep.normalize import dedupe_key, variant_key
from jobengine.sweep.runner import fake_deps, run_sweep


def test_engineer_is_left_out_when_keys_are_compared():
    a = dedupe_key("Lepaya B.V.", "DevSecOps", "Amsterdam", "Netherlands")
    b = dedupe_key("Lepaya", "DevSecOps Engineer", "Amsterdam", "Netherlands")
    assert a != b and variant_key(a) == variant_key(b) == "lepaya|devsecops|amsterdam"
    c = dedupe_key("Lepaya", "Inżynier DevSecOps", "Amsterdam", "Netherlands")
    assert variant_key(c) == variant_key(a)
    assert variant_key(dedupe_key("Acme", "Engineer", "Kraków", "Poland")) == "acme|engineer|krakow"
    other = dedupe_key("Lepaya", "Senior DevSecOps Engineer", "Amsterdam", "Netherlands")
    assert variant_key(other) != variant_key(a)  # seniority is another job
    assert variant_key("no pipes") == "no pipes"


def test_a_later_fetch_finds_the_row_under_the_other_title():
    s = settings()
    deps = fake_deps(s)
    run_sweep(s, deps, FAKE_TODAY, state=FakeBotState())
    repo = deps.repo
    repo.writes.clear()
    run_sweep(s, deps, FAKE_TODAY, state=FakeBotState())
    seen_again = {pid for kind, pid, _ in repo.writes if kind == "update"}
    before = len(repo.rows)
    # A row the next /fetch sees again loses "Engineer" in its title, as if another site
    # had named it so first.
    pid = next(p for p in sorted(seen_again)
               if " engineer|" in str(repo.rows[p].get("Dedupe key")))
    repo.rows[pid]["Dedupe key"] = repo.rows[pid]["Dedupe key"].replace(" engineer|", "|")
    repo.writes.clear()
    run_sweep(s, deps, FAKE_TODAY, state=FakeBotState())
    assert len(repo.rows) == before  # found under the other title: updated, not saved again
    assert any(kind == "update" and p == pid for kind, p, _ in repo.writes)
