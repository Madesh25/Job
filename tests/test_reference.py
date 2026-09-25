import pytest

from jobengine.reference import Reference, canon_term, split_terms

REF = Reference.fake()


@pytest.mark.parametrize(
    "term, kind, level",
    [
        ("Kubernetes", "skill", "Production"),
        ("kubernetes", "skill", "Production"),
        ("Terraform 1.5", "skill", "Production"),
        ("Prometheus", "skill", "Hands-on"),
        ("Amazon EKS", "term_map", None),
        ("GitHub Actions", "term_map", None),
    ],
)
def test_lookup_backed(term, kind, level):
    backing = REF.lookup(term)
    assert backing is not None
    assert (backing.kind, backing.level) == (kind, level)


@pytest.mark.parametrize("term", ["Go", "Rust", "Pulumi", "Nomad", "Consul", "Java", "Cobol", ""])
def test_lookup_not_backed(term):
    # Learning skills, Needs review rows, "(none)" rows and unknown terms never back a term.
    assert REF.lookup(term) is None


def test_none_rows_are_known_gaps_and_never_owned():
    assert REF.is_known_gap("Consul") and REF.is_known_gap("java")
    owned = REF.owned_terms()
    assert {"kubernetes", "prometheus", "github actions", "kubernetes on aws"} <= owned
    assert not {"go", "rust", "nomad", "java", "pulumi"} & owned


def test_canonical_terms():
    assert canon_term("CI/CD") == canon_term("CI CD") == "ci cd"
    assert canon_term("Python 3.11") == "python"
    assert canon_term("EC2") == "ec2"
    assert split_terms("Go, Golang; GoLang ") == ["Go", "Golang", "GoLang"]


def test_company_lookup_uses_canonical_names():
    assert REF.company("Vistula Cloud Sp. z o.o.").tier_number == 2
    assert REF.company("Harbour Soft").name == "Harbour Soft (IE)"
    assert REF.company("Tulip Data").ind_sponsor == "Verified"
    assert REF.company("Unknown Corp") is None
    assert REF.company(None) is None
