from datetime import date

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
        "Screening done: 14 screened (5 high, 1 normal, 2 low, 1 needs review, 5 skipped), "
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
        "ie-5-years": ("Apply high", None),
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
    assert row["Years required"] == 3
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
    assert deps.repo.rows["pl-polish-required"]["Language required"] == "Polish"
    assert deps.repo.rows["pl-polish-plus"]["Language required"] == "Polish preferred"
    assert deps.repo.rows["pl-polish-plus"]["Gaps"] == "Java"


def test_fabricated_quotes_are_not_written(s, deps):
    run(s, deps)
    row = deps.repo.rows["pl-fabricated"]
    assert "Years required" not in row
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


def test_llm_failure_is_reported_and_row_left_alone(s, deps, tmp_path):
    (tmp_path / "score").mkdir()
    from jobengine.llm import FakeLLM

    deps.llm = lambda config: FakeLLM(tmp_path)
    summary = run(s, deps)
    assert summary.screened == 0
    assert "Could not screen Vistula Cloud, DevOps Engineer: FakeLLM has no fixture" in (
        summary.text()
    )
    assert deps.repo.writes == []


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
    run(s, deps)
    assert len(llm.calls) == 14
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
