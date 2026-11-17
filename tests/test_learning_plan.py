"""Flow feature 14 (9 Oct): the learning plan under /gaps (which certificate first)."""

from test_telegram_bot import desk, settings, talk  # noqa: F401  (desk is a fixture)

from jobengine.screen import learning

CERTS = settings().learning["certificates"]


def test_covers_whole_words_only():
    assert learning.covers("azure", "Azure DevOps")
    assert learning.covers("argo cd", "Argo CD")
    assert not learning.covers("go", "Google Cloud")
    assert not learning.covers("aws", "Laws and regulations")


def test_the_certificate_covering_most_jobs_comes_first():
    jobs = [["Azure", "Python"], ["AKS", "Go"], ["Azure DevOps"], ["GCP"], ["Python"]]
    lines = learning.plan(jobs, CERTS)
    assert lines[1] == ("Learning plan (the certificate that covers gaps in the most jobs "
                        "first):")
    assert lines[2] == ("1. Microsoft Azure Administrator (AZ-104): 3 jobs (AKS, Azure, "
                        "Azure DevOps)")
    assert lines[3] == ("2. Google Cloud Associate Cloud Engineer: 1 job (GCP)")
    assert "No certificate covers: Python (2), Go (1). Learn them with a small project." in lines
    assert learning.plan([], CERTS) == [] and learning.plan(jobs, {}) == []


def test_gaps_command_has_the_plan_and_skips_none(desk):  # noqa: F811
    text = talk(desk, "/gaps").sent[0][1]
    assert "Learning plan (the certificate that covers gaps in the most jobs first):" in text
    assert "none: 1 job" not in text
