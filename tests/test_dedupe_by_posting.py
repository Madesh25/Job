"""16 Nov: Motorola renamed a job on its job board ("Junior" added) and the next /fetch
saved it again, although the old row named the same posting. A posting already on a row is
that row."""

from test_telegram_bot import settings

from jobengine.bot_state import FakeBotState
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.sweep.runner import fake_deps, run_sweep


def test_a_renamed_job_is_found_by_its_posting():
    s = settings()
    deps = fake_deps(s)
    run_sweep(s, deps, FAKE_TODAY, state=FakeBotState())
    repo = deps.repo
    repo.writes.clear()
    run_sweep(s, deps, FAKE_TODAY, state=FakeBotState())
    seen_again = sorted({pid for kind, pid, _ in repo.writes if kind == "update"})
    before = len(repo.rows)
    # A row seen again was saved under another title: its Dedupe key no longer matches,
    # its Posting IDs still name the posting.
    pid = seen_again[0]
    assert repo.rows[pid].get("Posting IDs")
    company, _title, place = str(repo.rows[pid]["Dedupe key"]).split("|")
    repo.rows[pid]["Dedupe key"] = f"{company}|an old name of the job|{place}"
    repo.writes.clear()
    run_sweep(s, deps, FAKE_TODAY, state=FakeBotState())
    assert len(repo.rows) == before  # updated, not saved again
    assert any(kind == "update" and p == pid for kind, p, _ in repo.writes)


def test_a_posting_without_an_id_matches_nothing():
    from jobengine.sweep.runner import _real_ref

    assert _real_ref("ats:workday-motorolasolutions-Software-Engineer_R68542")
    assert not _real_ref("jooble:") and not _real_ref("jooble:None") and not _real_ref("x")
