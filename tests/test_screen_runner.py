from datetime import date, timedelta

import pytest

from jobengine import http
from jobengine.llm import LLMError
from jobengine.screen import runner
from jobengine.screen.runner import (
    ScreenError,
    description,
    fake_deps,
    find_row,
    pending_rows,
    screen_one,
    screen_pending,
    waiting_for_jd,
)
from jobengine.settings import load_settings

TODAY = date(2026, 10, 1)


@pytest.fixture
def s():
    return load_settings("local", environ={})


@pytest.fixture
def deps(s):
    return fake_deps(s)


def run(s, deps):
    return screen_pending(s, deps, TODAY)


def verdicts(summary):
    return {r.page_id: (r.verdict, r.skip_reason) for r in summary.results}


def test_fake_run_summary(s, deps):
    summary = run(s, deps)
    assert summary.text() == (
        "Screening done: 14 screened (4 high, 1 normal, 2 low, 1 needs review, 6 skipped), "
        "1 waiting for JD."
    )


def test_fixture_verdicts(s, deps):
    got = verdicts(run(s, deps))
    assert got == {
        "pl-clean": ("Apply high", None),
        "pl-b2b": ("Skip", "B2B only"),
        "pl-polish-required": ("Skip", "Polish required"),
        "pl-polish-plus": ("Apply normal", None),
        "nl-ind": ("Apply high", None),
        "nl-not-register": ("Apply low", None),
        "ie-agency": ("Apply low", None),
        "pl-7-years": ("Skip", "Experience >5 yrs"),
        "ie-5-years": ("Skip", "Seniority"),  # 5 years: over the 4-year limit
        "pl-learning-go": ("Skip", "Tech mismatch"),
        "nl-sponsor-yes": ("Apply high", None),
        "pl-snippet": ("Needs review", None),
        "li-no-jd": ("Unscreened", None),
        "pl-fabricated": ("Apply high", None),
        "pl-already": ("Skip", "Already applied"),
    }


def test_only_unscreened_rows_are_touched(s, deps):
    run(s, deps)
    touched = {page_id for _, page_id, _ in deps.repo.writes}
    assert "already-screened" not in touched
    assert "applied-odra" not in touched
    assert "li-no-jd" not in touched  # waiting for the JD, left Unscreened
    assert deps.repo.rows["li-no-jd"]["Screen verdict"] == "Unscreened"


def test_written_properties_for_a_clean_job(s, deps):
    run(s, deps)
    row = deps.repo.rows["pl-clean"]
    assert row["Status"] == "Screened"
    assert row["Screen verdict"] == "Apply high"
    assert row["Language required"] == "English"
    assert row["Contract type"] == "UoP"
    assert row["Sponsorship"] == "Not mentioned"
    assert row["Work mode"] == "Hybrid"
    assert row["Salary"] == "16 000 - 21 000 PLN gross per month"
    assert row["Years required"] == "3+ years"
    assert row["Tech stack"] == ["Kubernetes", "Terraform", "AWS"]
    assert "Skip reason" not in row and "Gaps" not in row
    body = row["body"]
    start = body.index("Screening (V16, 2026-10-01)")
    assert body[start + 1:start + 5] == [
        "Strong | Kubernetes in production | Kubernetes",
        "Strong | Terraform | Terraform",
        "Strong | AWS | AWS",
        "Transferable | Prometheus | Prometheus",
    ]
    assert body[start + 5] == (
        "Specific details: moving our workloads to EKS ; running GitOps with Argo CD"
    )


def test_skip_rows_record_reason_and_gaps(s, deps):
    run(s, deps)
    go = deps.repo.rows["pl-learning-go"]
    assert (go["Screen verdict"], go["Skip reason"], go["Gaps"]) == ("Skip", "Tech mismatch", "Go")
    assert go["Contract type"] == "Both"
    polish = deps.repo.rows["pl-polish-required"]  # "Fluent Polish is required": no AI used
    assert (polish["Screen verdict"], polish["Skip reason"]) == ("Skip", "Polish required")
    assert deps.repo.rows["pl-polish-plus"]["Language required"] == "Polish preferred"
    assert deps.repo.rows["pl-polish-plus"]["Gaps"] == "Java"


def test_fabricated_quotes_are_not_written(s, deps):
    run(s, deps)
    row = deps.repo.rows["pl-fabricated"]
    assert row["Years required"] == "3 years"  # read from the description, not the AI
    assert row["Sponsorship"] == "Not mentioned"
    assert "Java" not in row["Tech stack"]


def test_visa_flags_are_merged_never_removed(s, deps):
    run(s, deps)
    assert deps.repo.rows["nl-ind"]["Visa flags"] == ["Salary below visa minimum",
                                                     "On IND register"]
    assert deps.repo.rows["nl-sponsor-yes"]["Visa flags"] == ["On IND register",
                                                             "Sponsorship stated"]
    assert deps.repo.rows["ie-agency"]["Visa flags"] == ["Agency posting (IE)"]
    assert "Visa flags" not in deps.repo.rows["pl-clean"]


def test_ind_register_unavailable(s, deps):
    def broken(url):
        raise http.HttpError("GET https://ind.nl failed: HTTP 503", 503)

    deps.get_text = broken
    summary = run(s, deps)
    assert "IND register unavailable" in summary.text()
    # Target Companies status still counts; the register lookup gives no flag at all.
    assert deps.repo.rows["nl-sponsor-yes"]["Visa flags"] == ["On IND register",
                                                             "Sponsorship stated"]
    assert "Visa flags" not in deps.repo.rows["nl-not-register"]


def test_rescreen_is_appended_and_status_never_moves_back(s, deps):
    run(s, deps)
    row = deps.repo.rows["pl-clean"]
    row["Status"] = "Applied"
    before = list(row["body"])
    summary = screen_one(s, deps, TODAY, "pl-clean")
    assert summary.screened == 1
    assert row["Status"] == "Applied"
    assert row["body"][:len(before)] == before  # never deletes blocks
    assert sum(1 for b in row["body"] if b.startswith("Screening (V16")) == 2


def test_status_only_moves_from_new(s, deps):
    deps.repo.rows["pl-clean"]["Status"] = "Approved"
    run(s, deps)
    assert deps.repo.rows["pl-clean"]["Status"] == "Approved"
    assert deps.repo.rows["pl-clean"]["Screen verdict"] == "Apply high"


def test_no_write_writes_nothing(s):
    deps = fake_deps(s, write=False)
    summary = run(s, deps)
    assert summary.screened == 14
    assert deps.repo.writes == []
    assert "--no-write" in summary.text()


def test_screen_one_no_write_says_so(s):
    deps = fake_deps(s, write=False)
    summary = screen_one(s, deps, TODAY, "pl-clean")
    assert summary.screened == 1
    assert deps.repo.writes == []
    assert "--no-write" in summary.text()


def test_cli_lists_a_row_given_without_dashes():
    from jobengine.screen.__main__ import page_key

    dashed = "3e66edc2-b0d4-811b-866f-d341365c7753"
    assert page_key(dashed) == page_key("3e66edc2b0d4811b866fd341365c7753")
    assert page_key("3E66EDC2B0D4811B866FD341365C7753") == page_key(dashed)


def test_llm_failure_is_reported_and_row_left_alone(s, deps, tmp_path):
    (tmp_path / "score").mkdir()
    from jobengine.llm import FakeLLM

    deps.llm = lambda config: FakeLLM(tmp_path)
    summary = run(s, deps)
    free = {r.page_id for r in summary.results if not r.llm_used}
    assert summary.screened == len(free)  # only the free seniority skips
    assert "Could not screen Vistula Cloud, DevOps Engineer: FakeLLM has no fixture" in (
        summary.text()
    )
    assert {w[1] for w in deps.repo.writes} == free
    deps.repo.writes.clear()


def test_refused_key_stops_screening_after_the_first_job(s, deps):
    from jobengine.llm import LLMAuthError

    class Refused:
        calls = 0

        def complete_json(self, *args, **kwargs):
            Refused.calls += 1
            raise LLMAuthError("Anthropic refused ANTHROPIC_API_KEY (HTTP 401).")

    deps.llm = lambda config: Refused()
    summary = run(s, deps)
    assert Refused.calls == 1
    text = summary.text()
    assert "Screening stopped: Anthropic refused ANTHROPIC_API_KEY (HTTP 401)." in text
    assert "reached" not in text  # not reported as the daily limit
    assert all("no AI used" in str(w) or "Skip reason" in str(w) for w in deps.repo.writes)


def test_missing_key_is_a_clear_error(s, deps):
    def no_key(config):
        raise LLMError("ANTHROPIC_API_KEY is not set, so LLM screening cannot run")

    deps.llm = no_key
    with pytest.raises(ScreenError, match="ANTHROPIC_API_KEY"):
        run(s, deps)


def test_prompts_carry_only_job_text(s, deps):
    from jobengine.llm import FakeLLM

    llm = FakeLLM()
    deps.llm = lambda config: llm
    summary = run(s, deps)
    free = [r.page_id for r in summary.results if not r.llm_used]
    assert sorted(free) == ["ie-5-years", "pl-7-years", "pl-b2b", "pl-polish-required"]
    assert len(llm.calls) == 14 - len(free) == 10
    for call in llm.calls:
        assert "@" not in call["user"] and "+48" not in call["user"]


def test_no_write_target_screens_nothing(s):
    prod = load_settings("prod", environ={"DRY_RUN": "true"})
    deps = fake_deps(prod)
    deps.repo = None
    assert "DRY RUN" in run(prod, deps).text()


def test_description_kinds():
    long = "x" * 700
    assert description([], 600) == ("", "none")
    assert description(["Description source: ats"], 600) == ("", "none")
    assert description(["Description source: ats", long], 600) == (long, "full")
    assert description(["Description source: ats", "short"], 600)[1] == "snippet"
    assert description(["Description source: adzuna (snippet only)", long], 600)[1] == "snippet"
    blocks = ["Description source: adzuna (snippet only)", "short",
              "Screening (V16, 2026-09-01)", "Gap | x | none",
              "Description source: pasted (full)", long[:400], long[400:]]
    assert description(blocks, 600) == (long, "full")


def test_find_row_by_id_url_and_posting_id(s, deps):
    assert find_row(deps.repo, "pl-clean").page_id == "pl-clean"
    assert find_row(deps.repo, "linkedin:4012345678").page_id == "li-no-jd"
    assert find_row(deps.repo, "https://jobs.example.com/nl-ind").page_id == "nl-ind"
    assert find_row(deps.repo, "linkedin:4012") is None
    assert find_row(deps.repo, "missing") is None


def test_pending_and_waiting_lists(s, deps):
    run(s, deps)
    pending = {r.page_id for r in pending_rows(deps.repo)}
    assert "pl-clean" in pending and "already-screened" in pending
    assert "pl-b2b" not in pending and "applied-odra" not in pending
    assert [r.page_id for r in waiting_for_jd(deps.repo)] == ["li-no-jd"]


def test_fixtures_are_invented():
    text = "".join(p.read_text(encoding="utf-8")
                   for p in (runner.FIXTURES / "descriptions").glob("*.txt"))
    assert "@" not in text


# ---------------------------------------------------------------- daily limit


def llm_sent(summary):
    return [r for r in summary.results if r.description_kind != "none" and r.llm_used]


def test_daily_limit_caps_the_run_and_is_shared_by_later_runs(s, deps):
    s.screening["daily_limit"] = 5
    first = run(s, deps)
    assert len(llm_sent(first)) == 5
    assert deps.state.get("screen.day") == {"date": TODAY.isoformat(), "count": 5}
    assert any("daily limit 5 reached (5 screened today)" in e for e in first.errors)

    second = run(s, deps)
    assert second.results == []
    assert any("daily limit 5 reached" in e for e in second.errors)

    tomorrow = screen_pending(s, deps, TODAY + timedelta(days=1))
    assert len(llm_sent(tomorrow)) == 5
    assert deps.state.get("screen.day")["count"] == 5


def test_newest_posting_is_screened_first(s, deps):
    s.screening["daily_limit"] = 1
    with_jd = [row.get("Posted date") or date.min for row in deps.repo.rows.values()
               if row.get("Screen verdict") == "Unscreened"
               and any(b.startswith("Description source:") for b in row.get("body") or [])]
    summary = run(s, deps)
    picked = llm_sent(summary)[0].page_id
    free = [deps.repo.rows[r.page_id].get("Posted date") or date.min
            for r in summary.results if not r.llm_used]
    newest = max(d for d in with_jd if d not in free or with_jd.count(d) > free.count(d))
    assert (deps.repo.rows[picked].get("Posted date") or date.min) == newest
    assert len({d for d in with_jd}) > 1  # the order is actually tested


def test_cli_limit_overrides_what_is_left_today(s, deps):
    s.screening["daily_limit"] = 2
    run(s, deps)
    summary = screen_pending(s, deps, TODAY, limit=3)
    assert len(llm_sent(summary)) == 3
    assert deps.state.get("screen.day")["count"] == 5


def test_no_write_does_not_use_up_the_day(s):
    s.screening["daily_limit"] = 3
    deps = fake_deps(s, write=False)
    assert len(llm_sent(run(s, deps))) == 3
    assert deps.state.get("screen.day") is None


def test_screen_one_counts_but_is_never_blocked(s, deps):
    s.screening["daily_limit"] = 1
    run(s, deps)
    summary = screen_one(s, deps, TODAY, "pl-clean")
    assert summary.screened == 1
    assert deps.state.get("screen.day")["count"] == 2


def test_screening_runs_several_jobs_at_once_after_the_first(s, deps):
    import threading
    import time

    from jobengine.llm import FakeLLM

    real = FakeLLM()
    running, peak, lock = [0], [0], threading.Lock()

    class Slow:
        calls = real.calls

        def complete_json(self, *args, **kwargs):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.03)
            try:
                return real.complete_json(*args, **kwargs)
            finally:
                with lock:
                    running[0] -= 1

    s.screening["workers"] = 4
    deps.llm = lambda config: Slow()
    summary = run(s, deps)
    assert summary.screened == 14  # the same result as one at a time
    assert 1 < peak[0] <= 4


def test_years_cell_is_raised_to_what_the_description_asks():
    from jobengine.screen.models import JobRow
    from jobengine.screen.runner import years_update

    jd = "IT: 5 lat doswiadczenia; w podobnej roli: 2 lata doswiadczenia"
    row = JobRow(page_id="p", company="Vistula Cloud", role="Cloud Engineer", years_required=2)
    assert years_update(row, jd) == {"Years required": "5 years"}
    assert years_update(JobRow(page_id="p", company="c", role="r", years_required=5), jd) == {}
    assert years_update(row, "Kubernetes and Terraform") == {}
