"""Old job descriptions tidied during /fetch, DEV only (2 Nov, your idea): the description
is rewritten in the same place on the page, everything else stays."""

from jobengine.bot_state import FakeBotState
from jobengine.notion_repo import FakeJobsRepo
from jobengine.settings import load_settings
from jobengine.sweep import tidy_bodies
from jobengine.sweep.models import SweepSummary

S = load_settings("local", {})
OLD_TEXT = ("Accountabilities<br />Ensure protocol reliability:<br />"
            + "Maintain networks. " * 200)


def old_page():
    """A page as written before 2 Nov: tags as text, cut every 2000 characters."""
    chunks = [OLD_TEXT[i:i + 2000] for i in range(0, len(OLD_TEXT), 2000)]
    return ["Description source: adzuna (snippet only)", "A short snippet...",
            "Earlier link (Adzuna): https://www.adzuna.pl/details/1",
            "Description source: company site (direct link)", *chunks,
            "Screening (V16, 2026-10-30)", "Verdict: Needs review"]


def test_an_old_description_is_rewritten_in_place():
    repo = FakeJobsRepo([{"page_id": "p1", "Swept date": "2026-10-30", "body": old_page()}])
    result = tidy_bodies.run(S, repo, FakeBotState())
    assert (result.checked, result.rewritten, result.left) == (1, 1, 0)
    body = repo.rows["p1"]["body"]
    start = body.index("Description source: company site (direct link)")
    end = body.index("Screening (V16, 2026-10-30)")
    new = body[start + 1:end]
    assert new[0] == "Accountabilities\nEnsure protocol reliability:"
    assert not any("<br" in b for b in new) and all(len(b) <= 2000 for b in new)
    assert all(b.startswith("Maintain") and b.endswith("networks.")  # no word cut in two
               for b in new[1:])
    # everything around it stays where it was
    assert body[:4] == old_page()[:4] and body[-2:] == old_page()[-2:]


def test_a_tidy_page_is_not_touched_and_pages_are_checked_once():
    tidy = ["Description source: ats", "Requirements:\n• Kubernetes\n• Terraform"]
    repo = FakeJobsRepo([{"page_id": "new", "Swept date": "2026-11-02", "body": tidy},
                         {"page_id": "old", "Swept date": "2026-10-01", "body": old_page()}])
    state = FakeBotState()
    result = tidy_bodies.run(S, repo, state)
    assert (result.checked, result.rewritten) == (2, 1)
    assert [w[1] for w in repo.writes] == ["old"]
    again = tidy_bodies.run(S, repo, state)
    assert (again.checked, again.rewritten, again.left) == (0, 0, 0)


def test_a_batch_per_fetch_newest_first():
    rows = [{"page_id": f"p{i}", "Swept date": f"2026-10-{10 + i}", "body": old_page()}
            for i in range(5)]
    repo = FakeJobsRepo(rows)
    s = load_settings("local", {})
    s.sweep["tidy_bodies"] = {"enabled": True, "max_pages": 2}
    result = tidy_bodies.run(s, repo, FakeBotState())
    assert (result.checked, result.left) == (2, 3)
    assert [w[1] for w in repo.writes] == ["p4", "p3"]


def test_on_in_local_and_dev_off_in_prod():
    assert tidy_bodies.enabled(load_settings("local", {}))
    assert tidy_bodies.enabled(load_settings("dev", {}))
    assert not tidy_bodies.enabled(load_settings("prod", {}))


def test_the_summary_says_how_far_it_is():
    text = SweepSummary(tidied=(60, 41, 520)).friendly_text()
    assert "Old descriptions tidied (DEV): 41 of 60 pages checked, 520 still to check" in text
    assert "all old pages done" in SweepSummary(tidied=(12, 3, 0)).friendly_text()
