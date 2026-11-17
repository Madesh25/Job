from datetime import date

import pytest

from jobengine import http
from jobengine.safety import SafetyError, check_target_companies_write
from jobengine.settings import load_settings
from jobengine.strategy import ind_refresh, runner

TODAY = date(2026, 10, 1)


def deps(env="local", fail=None):
    writes = []
    d = ind_refresh.fake_deps(load_settings(env, {}), writer=lambda pid, props: (
        check_target_companies_write(list(props)), writes.append((pid, props))))
    d.today = lambda: TODAY
    if fail:
        def broken(url):
            raise fail
        d.get_text = broken
    return d, writes


def test_prod_writes_only_the_two_fields_and_reports_changes():
    d, writes = deps("prod")
    report = ind_refresh.refresh(d)
    assert report.checked == 4 and report.written
    assert [(c.company, c.old, c.new) for c in report.changes] == [
        ("Canal Payments", "Not listed", "Verified"),
        ("Example Gone B.V.", "Verified", "Not listed")]
    assert {k for _, props in writes for k in props} == {"IND sponsor", "Last checked"}
    unchanged = dict(writes)["tc-tulip"]
    assert unchanged == {"Last checked": TODAY}  # Last checked always, sponsor only on change
    text = report.text()
    assert text.startswith("IND register: 4 NL companies checked, 2 changed (Canal Payments: "
                           "Not listed -> Verified; Example Gone B.V.: Verified -> Not listed).")
    assert "Warning: Example Gone B.V. is no longer on the IND register." in text


def test_outside_prod_nothing_is_written():
    d, writes = deps("dev")
    report = ind_refresh.refresh(d)
    assert writes == [] and not report.written
    assert "Not written outside prod." in report.text()


@pytest.mark.parametrize("error", [http.HttpError("download failed", status=503),
                                   ValueError("no organisation names found")])
def test_failed_download_changes_nothing(error):
    d, writes = deps("prod", fail=error)
    report = ind_refresh.refresh(d)
    assert writes == [] and report.error
    assert report.text().startswith("IND register check failed:")
    assert report.text().endswith("Nothing was changed.")


def test_target_companies_allowlist():
    check_target_companies_write(["IND sponsor", "Last checked", "ATS platform"])
    with pytest.raises(SafetyError):
        check_target_companies_write(["IND sponsor", "Tier"])


def test_update_runs_the_ind_refresh():
    d = runner.fake_deps(load_settings("local", {}))
    d.today = lambda: TODAY
    result = runner.run_update(d)
    assert result.messages[-1].startswith("IND register: 4 NL companies checked, 2 changed")
    assert d.ind_writes == []  # local: listed, not written


@pytest.mark.parametrize(("updated", "expected"), [
    ("2026-09-05", None),
    ("2026-09-04", "Strategy review due in 3 days. Run /update."),
    ("2026-09-02", "Strategy review due in 1 day. Run /update."),
    ("2026-09-01", "Strategy review due today. Run /update."),
    ("2026-08-25", "Strategy review is 7 days overdue: /fetch is blocked. Run /update."),
])
def test_monthly_reminder(updated, expected):
    from jobengine.config_store import ConfigStore

    d = runner.fake_deps(load_settings("prod", {}))
    d.config = lambda: ConfigStore.from_values(
        {"last_strategy_update": updated, "strategy_refresh_days": "30"})
    assert runner.run_strategy_reminder(d, TODAY) == expected
