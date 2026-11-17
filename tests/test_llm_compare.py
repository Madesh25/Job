from datetime import date

from jobengine import llm_compare as cmp
from jobengine.llm import LLMError, Usage

JD = "We need 5 years of Kubernetes experience. Fully remote. Terraform is a plus."
RESUME = "Madeshwaran M. DevOps Engineer. Kubernetes, Terraform, AWS. Open to relocate."
LETTER = ("Dear Hiring Team,\n\n" + "I run Kubernetes and Terraform on AWS every day. " * 14
          + "\n\nKind regards,\nMadeshwaran M")
EXTRACT = {"years_required_min": {"value": 5, "quote": "5 years of Kubernetes experience"},
           "work_mode": {"value": "Remote", "quote": "Fully remote"},
           "mandatory_requirements": [{"terms": ["Kubernetes"], "kind": "tool",
                                       "quote": "5 years of Kubernetes experience"}]}
# The second model gets the years wrong and makes up its quote.
WRONG = {**EXTRACT, "years_required_min": {"value": 4, "quote": "four years of cloud work"}}


def client(model, extract, fail_cover=False):
    def call(stage, system, user, max_tokens):
        if stage == "score":
            return extract
        if stage == "cover":
            if fail_cover:
                raise LLMError("Gemini call failed: HTTP 503 UNAVAILABLE")
            return {"letter": LETTER}
        return {"questions": [{"q": "How do you run Kubernetes upgrades?",
                               "hint": "Talk about Kubernetes on AWS."}]}
    return cmp.Pinned(model, call, Usage())


JOB = cmp.Job(page_id="abc-123", company="Tulip <b>", role="DevOps Engineer",
              country="Netherlands", jd=JD)


def run(tasks=("score", "cover", "prep")):
    ctx = cmp.Context(master_text=RESUME)
    return cmp.compare([JOB], [client("claude-haiku-4-5", EXTRACT),
                               client("gemini-3.5-flash-lite", WRONG, fail_cover=True)],
                       list(tasks), ctx)


def test_each_task_runs_with_both_models():
    report = run()
    base = report.get(JOB, "score", "claude-haiku-4-5")
    other = report.get(JOB, "score", "gemini-3.5-flash-lite")
    assert base.ok and base.facts["years"] == 5 and base.dropped == 0
    assert other.ok and other.facts["years"] is None and other.dropped == 1
    assert report.get(JOB, "cover", "claude-haiku-4-5").ok
    failed = report.get(JOB, "cover", "gemini-3.5-flash-lite")
    assert not failed.ok and "the AI call failed (LLMError)" in failed.problem
    assert report.get(JOB, "prep", "gemini-3.5-flash-lite").lines == [
        "How do you run Kubernetes upgrades? (hint: Talk about Kubernetes on AWS.)"]


def test_summary_counts_agreement_failures_and_dropped_quotes():
    report = run()
    assert cmp.agreement(report) == (5, 6)  # all but the years
    rows = {(r[0], r[1]): r for r in cmp.summary_rows(report)}
    assert rows[("score", "gemini-3.5-flash-lite")][5] == "1"
    assert rows[("cover", "gemini-3.5-flash-lite")][2:4] == ["0", "1"]
    text = cmp.summary_text(report)
    assert "Screening answers that agree: 5 of 6 (83%)" in text


def test_report_is_escaped_html():
    page = cmp.render_html(run(), date(2026, 10, 20))
    assert page.startswith("<!doctype html>") and "<title>AI comparison 2026-10-20</title>" in page
    assert "Tulip &lt;b&gt;" in page and "Tulip <b>" not in page
    assert "Failed: the AI call failed (LLMError)" in page


def test_a_crash_in_one_task_is_a_failed_answer():
    def build(llm, job):
        raise RuntimeError("Notion down")
    ctx = cmp.Context(build=build)
    report = cmp.compare([JOB], [client("a", EXTRACT)], ["resume"], ctx)
    answer = report.get(JOB, "resume", "a")
    assert not answer.ok and answer.problem == "RuntimeError: Notion down"


def test_unknown_task_is_refused(capsys):
    assert cmp.main(["--model", "gemini-3.5-flash-lite", "--tasks", "score,mail"]) == 2
    assert "Unknown task: mail" in capsys.readouterr().out
