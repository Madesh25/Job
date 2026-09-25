import logging
from datetime import date

import pytest

from jobengine.notion_repo import FakeJobsRepo
from jobengine.settings import load_settings
from jobengine.sweep import fakes
from jobengine.sweep.__main__ import main as sweep_main
from jobengine.sweep.models import RawPosting, SourceResult
from jobengine.sweep.runner import SweepDeps, SweepError, fake_deps, real_deps, run_sweep

TODAY = date(2026, 10, 1)
S = load_settings("local", {})


def rows_by_key(repo):
    return {row["Dedupe key"]: (page_id, row) for page_id, row in repo.rows.items()}


@pytest.fixture
def fake_run():
    deps = fake_deps(S)
    summary = run_sweep(S, deps, TODAY)
    return summary, deps.repo


def test_fake_sweep_summary(fake_run):
    summary, _ = fake_run
    assert summary.text().splitlines()[0] == (
        "Sweep done: 8 new, 1 updated, 5 reposts, 3 skipped (out of scope), "
        "1 high ghost risk. Sources: gmail 6, adzuna 7, ats 4. Not supported: 2 companies."
    )
    assert summary.text().splitlines()[-1] == "- Maas Logistics, Medior DevOps Engineer (Rotterdam)"


def test_same_job_from_adzuna_and_gmail_gives_one_row(fake_run):
    _, repo = fake_run
    _, row = rows_by_key(repo)["vistula cloud|devops engineer|krakow"]
    assert row["Times seen"] == 2
    assert row["Posting IDs"] == (
        "gmail:vistula-cloud-devops-engineer-krakow-devops, adzuna:4300000001"
    )
    # Created from the email (no salary), then the Adzuna salary and date filled the gaps.
    assert row["Salary"] == "18000 - 24000 PLN (Adzuna)"
    assert row["Posted date"] == date(2026, 9, 29)
    assert row["body"][0] == "Description source: adzuna (snippet only)"


def test_same_posting_again_does_not_increment(fake_run):
    _, repo = fake_run
    row = repo.rows["seed-seen-again"]
    assert row["Times seen"] == 1
    assert row["Swept date"] == TODAY
    assert row["Posting IDs"] == "adzuna:4300000002"
    assert "Salary" not in row  # the Adzuna salary was predicted


def test_status_and_screen_verdict_never_change(fake_run):
    _, repo = fake_run
    assert repo.rows["seed-seen-again"]["Status"] == "Screened"
    assert repo.rows["seed-seen-again"]["Screen verdict"] == "Apply normal"
    assert repo.rows["seed-old-ghost"]["Status"] == "Applied"
    assert repo.rows["seed-repost"]["Status"] == "New"
    for action, _, payload in repo.writes:
        if action == "update":
            assert "Status" not in payload and "Screen verdict" not in payload


def test_reposts_and_ghost_risk(fake_run):
    _, repo = fake_run
    repost = repo.rows["seed-repost"]
    assert repost["Times seen"] == 3  # ats + LinkedIn alert, two new posting IDs
    assert repost["First seen"] == date(2026, 8, 20)
    assert repost["Ghost job risk"] == "Medium"
    ghost = repo.rows["seed-old-ghost"]
    assert ghost["Times seen"] == 3 and ghost["Ghost job risk"] == "High"


def test_new_rows(fake_run):
    _, repo = fake_run
    rows = rows_by_key(repo)
    _, baltic = rows["baltic bank|junior devops engineer|gdansk"]
    assert baltic["Status"] == "New" and baltic["Screen verdict"] == "Unscreened"
    assert baltic["Seniority"] == "Junior" and baltic["Ghost job risk"] == "Medium"
    assert baltic["City"] == "Gdańsk" and baltic["Board"] == "Adzuna"
    _, tulip = rows["tulip data|platform engineer|den haag"]
    assert tulip["Board"] == "LinkedIn" and tulip["Times seen"] == 2
    assert tulip["Salary"] == "60000 - 75000 EUR per year salary (Lever)"
    _, liffey = rows["liffey analytics|site reliability engineer|cork"]
    assert liffey["Country"] == "Ireland" and liffey["Ghost job risk"] == "Unknown"
    assert "body" in liffey and liffey["body"] == []
    _, odra = rows["odra systems|mid cloud engineer|wroclaw"]
    assert odra["City"] == "Wrocław"
    assert not any("lead" in key for key in rows)  # Lead titles dropped
    assert not any("wawel" in key for key in rows)  # Berlin job dropped


def test_second_run_next_day_only_updates(fake_run):
    _, repo = fake_run
    deps = fake_deps(S)
    deps.repo = repo
    summary = run_sweep(S, deps, date(2026, 10, 2))
    assert summary.new == 0 and summary.reposts == 0
    assert summary.updated == 14
    assert repo.rows["seed-seen-again"]["Times seen"] == 1


def test_gate_blocks_before_any_source_or_write():
    calls = []
    deps = fake_deps(S)
    deps.gmail = lambda: calls.append("gmail") or SourceResult("gmail")
    summary = run_sweep(S, deps, date(2026, 10, 24))
    assert summary.text() == (
        "/fetch is blocked: strategy last updated 2026-09-23, older than 30 days. "
        "Run /update first."
    )
    assert calls == [] and deps.repo.writes == []


def test_missing_write_target_means_dry_run(caplog):
    s = S.model_copy(update={"notion_write": {}})
    with caplog.at_level(logging.WARNING):
        deps = fake_deps(s)
    assert deps.repo is None
    assert "DRY RUN: would write to job_opportunities" in caplog.text
    summary = run_sweep(s, deps, TODAY)
    assert summary.new == 11  # no index without a repo: the 3 seeded jobs count as new
    assert "DRY RUN: would write to job_opportunities (no rows written)" in summary.notes


def test_single_source_and_skipped_sources():
    deps = fake_deps(S)
    deps.adzuna = lambda: SourceResult(
        "adzuna", skipped_reason="adzuna skipped: ADZUNA_APP_ID or ADZUNA_APP_KEY missing"
    )
    summary = run_sweep(S, deps, TODAY, sources=["adzuna"])
    assert summary.sources == {"gmail": 0, "adzuna": 0, "ats": 0}
    assert "adzuna skipped: ADZUNA_APP_ID or ADZUNA_APP_KEY missing" in summary.notes


def test_real_mode_skips_sources_without_secrets():
    s = load_settings("local", {"NOTION_TOKEN": "dummy"})
    deps = real_deps(s)
    assert deps.gmail().skipped_reason == "gmail skipped: GMAIL_ALERTS_TOKEN_JSON missing"
    assert deps.adzuna().skipped_reason.startswith("adzuna skipped")
    with pytest.raises(SweepError, match="NOTION_TOKEN missing"):
        real_deps(S)


def test_dev_writes_go_to_sandbox_through_safety():
    dev = load_settings("dev", {})
    assert fake_deps(dev).repo is not None
    prod_like = dev.model_copy(update={"notion_write": {"job_opportunities":
                                                        "0228ce56-475a-4b60-8d6b-fd2a59297b24"}})
    assert fake_deps(prod_like).repo is None  # a prod ID is never a write target outside prod


def test_cli_fake_run(capsys, monkeypatch):
    monkeypatch.setattr("jobengine.sweep.__main__.get_settings", lambda: S)
    assert sweep_main(["--fake", "--today", "2026-10-01"]) == 0
    out = capsys.readouterr().out
    assert "Sweep done: 8 new" in out


def test_cli_parse_report_writes_nothing(capsys, monkeypatch):
    monkeypatch.setattr("jobengine.sweep.__main__.get_settings", lambda: S)
    written = []
    monkeypatch.setattr(FakeJobsRepo, "create", lambda *a: written.append(a))
    assert sweep_main(["--fake", "--parse-report"]) == 0
    out = capsys.readouterr().out
    assert "board=IrishJobs.ie | title=Site Reliability Engineer" in out
    assert written == []


def test_cli_blocked_gate_exit_code(capsys, monkeypatch):
    monkeypatch.setattr("jobengine.sweep.__main__.get_settings", lambda: S)
    assert sweep_main(["--fake", "--today", "2026-11-30"]) == 2


def test_description_filled_on_existing_row_without_body():
    repo = FakeJobsRepo([{
        "page_id": "p1", "Dedupe key": "acme|devops engineer|warszawa",
        "Posting IDs": "gmail:x", "Times seen": 1, "First seen": date(2026, 9, 30),
    }])
    posting = RawPosting(
        source="adzuna", board="Adzuna", title="DevOps Engineer", company="Acme",
        location_text="Warszawa", url="https://example.com/1", posting_id="1",
        description="Kubernetes", description_is_snippet=True,
    )
    deps = SweepDeps(
        config=fakes.config, companies=lambda: [], repo=repo,
        gmail=lambda: SourceResult("gmail"),
        adzuna=lambda: SourceResult("adzuna", postings=[posting, posting]),
        ats=lambda companies, keep: SourceResult("ats"),
    )
    run_sweep(S, deps, TODAY)
    appends = [w for w in repo.writes if w[0] == "append_body"]
    assert len(appends) == 1
    assert appends[0][2] == ["Description source: adzuna (snippet only)", "Kubernetes"]


def test_existing_non_email_rows_skip_the_body_check():
    repo = FakeJobsRepo([{
        "page_id": "p1", "Dedupe key": "acme|devops engineer|warszawa",
        "Posting IDs": "adzuna:1", "Times seen": 1, "First seen": date(2026, 9, 30),
    }])
    checked = []
    repo.has_body = lambda page_id: checked.append(page_id) or True
    posting = RawPosting(
        source="adzuna", board="Adzuna", title="DevOps Engineer", company="Acme",
        location_text="Warszawa", url="https://example.com/1", posting_id="1",
        description="Kubernetes", description_is_snippet=True,
    )
    deps = SweepDeps(
        config=fakes.config, companies=lambda: [], repo=repo,
        gmail=lambda: SourceResult("gmail"),
        adzuna=lambda: SourceResult("adzuna", postings=[posting]),
        ats=lambda companies, keep: SourceResult("ats"),
    )
    summary = run_sweep(S, deps, TODAY)
    assert summary.updated == 1
    assert checked == []  # created by Adzuna, so it already has a description


def test_friendly_summary_counts_new_jobs_per_country(fake_run):
    summary, _ = fake_run
    assert summary.new_by_country == {"Netherlands": 2, "Poland": 4, "Ireland": 2}
    text = summary.friendly_text()
    lines = text.splitlines()
    assert lines[0] == "\u2705 Job search finished"
    # Countries follow Config countries.active order, each with its flag.
    assert lines[3:6] == [
        "\U0001F1F5\U0001F1F1 Poland: 4",
        "\U0001F1F3\U0001F1F1 Netherlands: 2",
        "\U0001F1EE\U0001F1EA Ireland: 2",
    ]
    assert "- Maas Logistics, Medior DevOps Engineer (Rotterdam)" in lines
    assert "Not checked this time" not in text


def test_friendly_summary_lists_sources_not_checked():
    deps = fake_deps(S)
    deps.gmail = lambda: SourceResult(
        "gmail", skipped_reason="gmail skipped: GMAIL_ALERTS_TOKEN_JSON missing"
    )
    text = run_sweep(S, deps, TODAY).friendly_text()
    assert "\u2139\uFE0F Not checked this time:\n- Email alerts: not set up yet" in text
    assert "Ireland: 1" in text  # zero-count countries would still be listed


def test_progress_lines_are_plain_language():
    lines = []
    run_sweep(S, fake_deps(S), TODAY, progress=lines.append)
    assert lines == [
        "Checking what is already in your Notion...",
        "Searching Email alerts... (0 jobs found so far)",
        "Searching Adzuna... (6 jobs found so far)",
        "Searching Company career sites... (13 jobs found so far)",
        "Found 17 jobs. Saving to your Notion...",
    ]


def test_failing_progress_callback_does_not_stop_the_sweep():
    def broken(line):
        raise RuntimeError("telegram down")

    assert run_sweep(S, fake_deps(S), TODAY, progress=broken).new == 8


def test_blocked_friendly_text_is_the_gate_message():
    summary = run_sweep(S, fake_deps(S), date(2026, 11, 30))
    assert summary.friendly_text() == summary.text()
