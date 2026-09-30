"""FETCH 10: every posting of a /fetch is counted per source, dropped ones with the reason."""

from test_runner import TODAY, S
from test_telegram_bot import Clock, settings

from jobengine import http
from jobengine import telegram_bot as tb
from jobengine.bot_state import FakeBotState
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.sweep.models import ALREADY, MERGED, SAVED, LossReport
from jobengine.sweep.runner import LAST_REPORT, SOURCE_LABELS, fake_deps, run_sweep


def sweep(deps=None):
    state = FakeBotState()
    summary = run_sweep(S, deps or fake_deps(S), TODAY, state=state)
    return summary, state


def test_every_posting_lands_in_one_bucket():
    summary, _ = sweep()
    for source, read in summary.sources.items():
        assert summary.loss.read(source) == read, source
    total = sum(summary.sources.values())
    assert sum(summary.loss.read(s) for s in summary.loss.by_source) == total
    saved = sum(c.get(SAVED, 0) for c in summary.loss.by_source.values())
    assert saved == summary.new
    # A merged posting (the same new job from a second source) is an update of the new row.
    kept = sum(c.get(ALREADY, 0) + c.get(MERGED, 0) for c in summary.loss.by_source.values())
    assert kept == summary.updated + summary.reposts


def test_summary_has_the_per_source_lines():
    summary, _ = sweep()
    text = summary.friendly_text()
    assert "Where the postings went (per source; /fetchreport lists the dropped jobs):" in text
    assert ("- Email alerts: 6 read: 4 saved as a new job, 1 already in Notion (updated), "
            "1 senior or other excluded title") in text


def test_dropped_jobs_keep_their_reason_and_source():
    summary, state = sweep()
    berlin = "Kubernetes Platform Engineer | Wawel Soft | Berlin, Deutschland"
    assert ("adzuna", "place not recognised", berlin) in summary.loss.dropped
    stored = LossReport.from_state(state.get(LAST_REPORT))
    assert stored.by_source == summary.loss.by_source
    assert stored.dropped == summary.loss.dropped


def test_report_text_and_word_filter():
    summary, state = sweep()
    report = LossReport.from_state(state.get(LAST_REPORT))
    text = report.report_text(dict(SOURCE_LABELS), "2026-10-01")
    assert text.startswith("Last /fetch (2026-10-01), where the postings went:")
    assert "\nsenior or other excluded title (2):\n" in text
    only = report.report_text(dict(SOURCE_LABELS), "2026-10-01", "place")
    assert "place not recognised (1):" in only and "posted too long ago" not in only
    assert "- Kubernetes Platform Engineer | Wawel Soft | Berlin, Deutschland [Adzuna]" in only
    none = report.report_text(dict(SOURCE_LABELS), "2026-10-01", "zzz")
    assert none.endswith("No dropped jobs match 'zzz'.")


def test_a_refused_row_is_counted_not_lost():
    deps = fake_deps(S)
    real = deps.repo.create
    calls = []

    def create(plan, blocks):
        calls.append(plan)
        if len(calls) == 1:
            raise http.HttpError("POST /v1/pages failed: HTTP 400", 400)
        return real(plan, blocks)

    deps.repo.create = create
    summary, _ = sweep(deps)
    refused = sum(c.get("Notion refused the row", 0) for c in summary.loss.by_source.values())
    assert refused >= 1
    for source, read in summary.sources.items():
        assert summary.loss.read(source) == read


def test_fetchreport_command():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    assert d.fetchreport_command()[0].text.startswith("No /fetch has run yet")
    summary, _ = sweep()
    d.state.set(LAST_REPORT, summary.loss.to_state("2026-10-01"))
    text = d.fetchreport_command("adzuna")[0].text
    assert "(matching 'adzuna')" in text and "[Adzuna]" in text
    assert "fetchreport" in {c["command"] for c in tb.bot_commands()}
