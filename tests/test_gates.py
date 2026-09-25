from datetime import date

import pytest

from jobengine.reference import Reference
from jobengine.screen.gates import run_gates, unbacked_tools
from jobengine.screen.models import Extraction, JobRow, Language, Quoted, Requirement

REF = Reference.fake()
TODAY = date(2026, 10, 1)


def row(**kw):
    base = dict(page_id="p1", company="Vistula Cloud", role="DevOps Engineer", country="Poland",
                dedupe_key="vistula cloud|devops engineer|krakow", status="New")
    base.update(kw)
    return JobRow(**base)


def tool(*terms):
    return Requirement(text=" / ".join(terms), terms=list(terms), kind="tool", quote="q")


def ext(**kw):
    base = dict(mandatory_requirements=[tool("Kubernetes"), tool("Terraform")])
    base.update(kw)
    return Extraction(**base)


def gate(r=None, e=None, others=()):
    return run_gates(r or row(), e or ext(), REF, list(others), TODAY)


def test_clean_row_survives():
    assert gate() is None


@pytest.mark.parametrize("title", ["Lead DevOps Engineer", "Principal SRE", "Head of Platform",
                                   "Staff Cloud Engineer"])
def test_senior_title_skips(title):
    hit = gate(row(role=title))
    assert (hit.gate, hit.reason) == (1, "Seniority")


def test_seniority_flag_from_description():
    hit = gate(e=ext(seniority_title_flag=Quoted(value="principal", quote="q")))
    assert hit.reason == "Seniority"


def test_years_over_five_skip_exactly_five_passes():
    assert gate(e=ext(years_required_min=Quoted(value=6, quote="q"))).reason == "Experience >5 yrs"
    assert gate(row(years_required=7)).reason == "Experience >5 yrs"
    assert gate(e=ext(years_required_min=Quoted(value=5, quote="q"))) is None


def test_mandatory_polish_or_dutch_skips_preferred_does_not():
    polish = Language(language="Polish", level="mandatory", quote="q")
    dutch = Language(language="Dutch", level="mandatory", quote="q")
    assert gate(e=ext(languages=[polish])).reason == "Polish required"
    assert gate(e=ext(languages=[dutch])).reason == "Dutch required"
    preferred = Language(language="Polish", level="preferred", quote="q")
    assert gate(e=ext(languages=[preferred])) is None


def test_b2b_only_in_poland():
    b2b = [Quoted(value="B2B", quote="q")]
    assert gate(e=ext(contract_types=b2b)).reason == "B2B only"
    both = [Quoted(value="B2B", quote="q"), Quoted(value="UoP", quote="q")]
    assert gate(e=ext(contract_types=both)) is None
    assert gate(row(country="Netherlands", company="Tulip Data B.V."),
                ext(contract_types=b2b)) is None


def test_tech_mismatch_writes_gaps():
    e = ext(mandatory_requirements=[tool("Kubernetes"), tool("Java"), tool("Nomad", "Consul")])
    hit = gate(e=e)
    assert (hit.gate, hit.reason) == (4, "Tech mismatch")
    assert hit.gaps == ("Java", "Nomad", "Consul")


def test_tech_one_backed_term_is_enough_and_learning_is_not():
    assert unbacked_tools(ext(mandatory_requirements=[tool("Go", "Python")]), REF) == []
    assert unbacked_tools(ext(mandatory_requirements=[tool("Go")]), REF) == ["Go"]


def test_non_tool_and_nice_to_have_never_trigger_tech_gate():
    e = ext(mandatory_requirements=[Requirement(text="CS degree", kind="education", quote="q")],
            nice_to_have=[tool("Java")])
    assert gate(e=e) is None


def test_already_applied_by_key_or_company_and_role():
    applied = row(page_id="p0", status="Applied")
    assert gate(others=[applied]).reason == "Already applied"
    other_city = row(page_id="p0", dedupe_key="other", status="Interview",
                     company="Vistula Cloud Sp. z o.o.")
    assert gate(others=[other_city]).reason == "Already applied"
    assert gate(others=[row(page_id="p0", status="New")]) is None
    assert gate(others=[row(status="Applied")]) is None  # the row itself


def test_expired():
    assert gate(e=ext(expires=Quoted(value="2026-09-30", quote="q"))).reason == "Expired"
    assert gate(row(expires=date(2026, 9, 1))).reason == "Expired"
    assert gate(row(expires=TODAY)) is None


def test_first_gate_wins():
    e = ext(languages=[Language(language="Polish", level="mandatory", quote="q")],
            mandatory_requirements=[tool("Java")])
    assert gate(row(role="Lead DevOps"), e).gate == 1
    assert gate(e=e).gate == 2
