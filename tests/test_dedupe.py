from datetime import date

from jobengine.sweep.dedupe import (
    MAX_POSTING_IDS,
    NEVER_UPDATED,
    IndexRow,
    create_plan,
    description_blocks,
    format_posting_ids,
    parse_posting_ids,
    update_plan,
)
from jobengine.sweep.models import Job

TODAY = date(2026, 10, 1)


def job(**kw):
    base = dict(
        source="adzuna",
        board="Adzuna",
        company="Acme",
        role="DevOps Engineer",
        city="Warszawa",
        country="Poland",
        url="https://example.com/a",
        posted_date=None,
        salary=None,
        seniority="Unknown",
        years_required=None,
        dedupe_key="acme|devops engineer|warszawa",
        posting_ref="adzuna:1",
        description=None,
        description_is_snippet=False,
    )
    base.update(kw)
    return Job(**base)


def row(**kw):
    base = dict(
        page_id="page-1",
        dedupe_key="acme|devops engineer|warszawa",
        posting_ids=["adzuna:1"],
        times_seen=1,
        first_seen=date(2026, 9, 30),
        url="https://example.com/a",
    )
    base.update(kw)
    return IndexRow(**base)


def test_create_plan_sets_defaults_and_skips_empty_values():
    plan = create_plan(job(posted_date=date(2026, 9, 29)), TODAY)
    assert plan["Status"] == "New"
    assert plan["Screen verdict"] == "Unscreened"
    assert plan["Times seen"] == 1
    assert plan["First seen"] == plan["Swept date"] == TODAY
    assert plan["Posting IDs"] == "adzuna:1"
    assert plan["Ghost job risk"] == "Low"
    assert "Salary" not in plan and "Years required" not in plan


def test_same_posting_next_day_does_not_increment():
    result = update_plan(row(), job(), TODAY)
    assert result.repost is False
    assert result.props == {"Swept date": TODAY, "Ghost job risk": "Unknown"}


def test_new_posting_id_increments_and_appends():
    result = update_plan(row(), job(source="gmail", posting_ref="gmail:9"), TODAY)
    assert result.repost is True
    assert result.props["Times seen"] == 2
    assert result.props["Posting IDs"] == "adzuna:1, gmail:9"
    assert result.props["Swept date"] == TODAY


def test_update_fills_empty_fields_only():
    existing = row(url=None, salary="10000 PLN", posted_date=None)
    result = update_plan(
        existing,
        job(url="https://example.com/b", salary="99999 PLN", posted_date=date(2026, 9, 20)),
        TODAY,
    )
    assert result.props["URL"] == "https://example.com/b"
    assert result.props["Posted date"] == date(2026, 9, 20)
    assert "Salary" not in result.props


def test_update_never_touches_status_screen_verdict_or_first_seen():
    for candidate in (job(), job(posting_ref="gmail:2"), job(url="x", salary="1 PLN")):
        props = update_plan(row(), candidate, TODAY).props
        assert not NEVER_UPDATED & props.keys()
        assert "First seen" not in props


def test_update_recomputes_ghost_risk():
    old = row(times_seen=2, first_seen=date(2026, 7, 1), posting_ids=["adzuna:1", "gmail:1"])
    result = update_plan(old, job(posting_ref="ats:5"), TODAY)
    assert result.props["Times seen"] == 3
    assert result.props["Ghost job risk"] == "High"


def test_posting_ids_keep_last_fifty():
    ids = [f"adzuna:{n}" for n in range(MAX_POSTING_IDS)]
    result = update_plan(row(posting_ids=ids, times_seen=50), job(posting_ref="gmail:new"), TODAY)
    kept = parse_posting_ids(result.props["Posting IDs"])
    assert len(kept) == MAX_POSTING_IDS
    assert kept[-1] == "gmail:new" and "adzuna:0" not in kept
    assert format_posting_ids(["a", "b"]) == "a, b"


def test_description_blocks():
    assert description_blocks(job()) == []
    blocks = description_blocks(job(description="x" * 4500, description_is_snippet=True))
    assert blocks[0] == "Description source: adzuna (snippet only)"
    assert [len(b) for b in blocks[1:]] == [2000, 2000, 500]
    huge = description_blocks(job(description="y" * 2000 * 200))
    assert len(huge) == 90
