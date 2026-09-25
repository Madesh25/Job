import json

from jobengine.llm import FakeLLM
from jobengine.screen.extract import SYSTEM_PROMPT, check_quotes, detail_ok, extract, quote_found

JD = """We are looking for a DevOps Engineer to join our platform team in Krakow.
You have at least 3 years of experience with Kubernetes in production and Terraform.
Experience with Prometheus is a plus. English is required, Polish is nice to have.
We offer a contract of employment (UoP) or B2B. Hybrid work, 2 days in the office.
Salary: 18 000 - 24 000 PLN gross per month. You will be moving our workloads to EKS
and running GitOps with Argo CD."""


def raw(**overrides):
    base = {
        "years_required_min": {"value": 3, "quote": "at least 3 years"},
        "seniority_title_flag": {"value": "none", "quote": None},
        "languages": [
            {"language": "English", "level": "mandatory", "quote": "English is required"},
            {"language": "Polish", "level": "preferred", "quote": "Polish is nice to have"},
        ],
        "contract_types": [
            {"value": "UoP", "quote": "contract of employment (UoP)"},
            {"value": "B2B", "quote": "or B2B"},
        ],
        "mandatory_requirements": [
            {"text": "Kubernetes in production", "terms": ["Kubernetes"], "kind": "tool",
             "quote": "Kubernetes in production"},
        ],
        "nice_to_have": [
            {"text": "Prometheus", "terms": ["Prometheus"], "kind": "tool",
             "quote": "Experience with Prometheus is a plus"},
        ],
        "sponsorship": {"value": "not_mentioned", "quote": None},
        "work_mode": {"value": "Hybrid", "quote": "Hybrid work"},
        "salary_text": {"value": "18 000 - 24 000 PLN gross per month",
                        "quote": "18 000 - 24 000 PLN gross per month"},
        "expires": {"value": None, "quote": None},
        "agency_posting": {"value": None, "quote": None},
        "specific_details": ["moving your workloads to EKS", "running GitOps with Argo CD"],
    }
    base.update(overrides)
    return base


def test_quote_found_is_whitespace_and_case_insensitive():
    assert quote_found("AT LEAST 3   years", JD)
    assert quote_found("workloads to EKS and running", JD)  # spans a line break
    assert not quote_found("at least 5 years", JD)
    assert not quote_found("", JD)
    assert not quote_found(None, JD)


def test_all_quoted_values_are_kept():
    ext, dropped = check_quotes(raw(), JD)
    assert dropped == []
    assert ext.years == 3
    assert ext.contract_values == {"UoP", "B2B"}
    assert ext.work_mode.value == "Hybrid"
    assert [r.terms for r in ext.mandatory_requirements] == [["Kubernetes"]]
    assert ext.specific_details == ["moving your workloads to EKS", "running GitOps with Argo CD"]


def test_fabricated_quotes_are_dropped():
    ext, dropped = check_quotes(
        raw(
            years_required_min={"value": 7, "quote": "at least 7 years"},
            sponsorship={"value": "stated_yes", "quote": "we sponsor visas"},
            mandatory_requirements=[
                {"text": "Kubernetes", "terms": ["Kubernetes"], "kind": "tool",
                 "quote": "Kubernetes in production"},
                {"text": "Java", "terms": ["Java"], "kind": "tool", "quote": "5 years of Java"},
            ],
        ),
        JD,
    )
    assert ext.years is None
    assert ext.sponsorship_value == "not_mentioned"
    assert [r.text for r in ext.mandatory_requirements] == ["Kubernetes"]
    assert set(dropped) == {"years_required_min", "sponsorship", "mandatory_requirements[1]"}


def test_values_without_quote_are_dropped():
    ext, dropped = check_quotes(raw(salary_text={"value": "30 000 PLN", "quote": None}), JD)
    assert ext.salary_text.value is None
    assert "salary_text" in dropped


def test_invalid_items_are_dropped_not_fatal():
    ext, dropped = check_quotes(raw(languages=[{"level": "mandatory"}], contract_types="x"), JD)
    assert ext.languages == []
    assert ext.contract_types == []
    assert "languages[0] invalid" in dropped


def test_specific_details_rules():
    assert detail_ok("moving your workloads to EKS", JD)
    assert not detail_ok("building a quantum compiler in Haskell", JD)
    assert not detail_ok("one two three four five six seven eight nine", JD)
    ext, _ = check_quotes(
        raw(specific_details=["moving workloads to EKS", "running GitOps with Argo CD",
                              "Kubernetes in production", "Terraform"]),
        JD,
    )
    assert len(ext.specific_details) == 3


def test_extract_uses_score_stage_and_only_job_text(tmp_path):
    (tmp_path / "score").mkdir()
    (tmp_path / "score" / "job1.json").write_text(json.dumps(raw()), encoding="utf-8")
    llm = FakeLLM(tmp_path)
    ext, dropped = extract(llm, title="DevOps Engineer", company="Vistula Cloud",
                           country="Poland", jd=JD, key="job1")
    assert ext.years == 3 and dropped == []
    call = llm.calls[0]
    assert call["stage"] == "score"
    assert call["system"] == SYSTEM_PROMPT
    assert "DevOps Engineer" in call["user"] and "Vistula Cloud" in call["user"]
    assert "@" not in call["user"]


def test_system_prompt_has_no_long_dashes():
    assert not {chr(0x2013), chr(0x2014)} & set(SYSTEM_PROMPT)
