from datetime import date

from jobengine.reference import Reference
from jobengine.screen import visa
from jobengine.screen.models import Extraction, JobRow, Quoted, Requirement
from jobengine.screen.tiering import (
    build_matrix,
    contract_type,
    employer_size,
    gap_count,
    permanent_contract,
    rank_key,
    tier,
)

REF = Reference.fake()
TODAY = date(2026, 10, 1)


def req(text, *terms, kind="tool"):
    return Requirement(text=text, terms=list(terms), kind=kind, quote="q")


def row(**kw):
    base = dict(page_id="p1", company="Vistula Cloud", role="DevOps Engineer", country="Poland")
    base.update(kw)
    return JobRow(**base)


UOP = [Quoted(value="UoP", quote="q")]


def ext(gaps=0, **kw):
    nice = [req(f"Gap {i}", "Cobol", kind="other") for i in range(gaps)]
    base = dict(mandatory_requirements=[req("Kubernetes", "Kubernetes")], nice_to_have=nice,
                contract_types=UOP)
    base.update(kw)
    return Extraction(**base)


def run(r=None, e=None, flags=(), kind="full"):
    r, e = r or row(), e or ext()
    return tier(r, e, REF, build_matrix(e, REF), list(flags), kind)


def test_matrix_strengths():
    e = Extraction(
        mandatory_requirements=[req("Kubernetes", "Kubernetes"), req("Prometheus", "Prometheus"),
                                req("EKS", "Amazon EKS"), req("CS degree", kind="education")],
        nice_to_have=[req("Go", "Go"), req("Java", "Java")],
    )
    matrix = build_matrix(e, REF)
    assert [(m.strength, m.backing) for m in matrix] == [
        ("Strong", "Kubernetes"),
        ("Transferable", "Prometheus"),
        ("Strong", "Kubernetes on AWS"),
        ("Gap", None),
        ("Gap", None),
        ("Gap", None),
    ]
    assert gap_count(matrix) == 3


def test_apply_high_needs_zero_gaps_large_employer_and_permanent():
    assert run().verdict == "Apply high"
    assert run(e=ext(contract_types=[Quoted(value="B2B", quote="q")])).verdict == "Apply normal"
    # Odra Systems is Tier 5: normal employer.
    assert run(row(company="Odra Systems S.A.")).verdict == "Apply normal"


def test_base_tier_by_gap_count():
    r = row(company="Odra Systems S.A.")
    assert [run(r, ext(gaps=n)).verdict for n in range(5)] == [
        "Apply normal", "Apply normal", "Apply low", "Needs review", "Needs review"]


def test_sponsorship_stated_moves_up_and_flags():
    yes = Quoted(value="stated_yes", quote="q")
    result = run(row(company="Odra Systems S.A."), ext(gaps=2, sponsorship=yes))
    assert result.verdict == "Apply normal"
    assert result.flags == (visa.SPONSORSHIP_STATED,)
    assert run(row(company="Odra Systems S.A."), ext(sponsorship=yes)).verdict == "Apply high"


def test_weak_employer_is_capped_at_apply_low():
    result = run(row(company="Unknown Startup"))
    assert (result.employer, result.verdict) == ("weak", "Apply low")


def test_unknown_company_with_sponsorship_is_not_weak():
    yes = Quoted(value="stated_yes", quote="q")
    result = run(row(company="Unknown Startup"), ext(sponsorship=yes))
    assert result.employer == "normal"
    assert result.verdict == "Apply high"


def test_cannot_sponsor_goes_to_bottom():
    no = Quoted(value="stated_no", quote="q")
    result = run(e=ext(sponsorship=no))
    assert (result.verdict, result.bottom) == ("Apply low", True)
    # Polder Logistics is Tier 6 (large) but Not on IND register: still Apply low, at the bottom.
    nl = row(company="Polder Logistics", country="Netherlands")
    result = run(nl, ext(contract_types=[]), flags=[visa.NOT_ON_IND])
    assert (result.employer, result.verdict, result.bottom) == ("large", "Apply low", True)
    result = run(row(company="Unknown BV", country="Netherlands"), ext(contract_types=[]),
                 flags=[visa.NOT_ON_IND])
    assert (result.employer, result.verdict, result.bottom) == ("weak", "Apply low", True)
    result = run(row(company="Odra Systems S.A."), ext(gaps=3, sponsorship=no))
    assert (result.verdict, result.bottom) == ("Needs review", True)


def test_snippet_survivor_needs_review():
    result = run(kind="snippet")
    assert result.verdict == "Needs review"
    assert "paste the full JD with /jd" in result.notes[0]


def test_employer_size_and_contract_rules():
    assert employer_size(row(company="Unknown", country="Netherlands"), ext(), REF,
                         [visa.ON_IND]) == "large"
    assert employer_size(row(company="Odra Systems"), ext(), REF, [visa.AGENCY_IE]) == "weak"
    both = [Quoted(value="UoP", quote="q"), Quoted(value="B2B", quote="q")]
    assert contract_type("Poland", ext(contract_types=both)) == "Both"
    assert contract_type("Netherlands", ext(contract_types=both)) == "Unknown"
    assert permanent_contract("Poland", ext(contract_types=both))
    assert permanent_contract("Ireland", ext(contract_types=[]))
    assert not permanent_contract("Ireland", ext(contract_types=[Quoted(value="contract",
                                                                        quote="q")]))


def test_rank_order():
    e = ext()
    good = rank_key("Apply normal", False, row(years_required=3, posted_date=date(2026, 9, 29)),
                    e, REF, TODAY)
    five = rank_key("Apply normal", False, row(years_required=5), e, REF, TODAY)
    ghost = rank_key("Apply normal", False, row(ghost_risk="High"), e, REF, TODAY)
    bottom = rank_key("Apply normal", True, row(years_required=3), e, REF, TODAY)
    high = rank_key("Apply high", False, row(ghost_risk="High"), e, REF, TODAY)
    review = rank_key("Needs review", False, row(years_required=3), e, REF, TODAY)
    ordered = sorted([review, bottom, ghost, five, good, high], reverse=True)
    assert ordered == [high, good, five, ghost, bottom, review]
