"""Small fixes from the Phase 3 test (5 Oct)."""

from jobengine.screen.desk import short_link
from jobengine.screen.models import join_gaps, split_gaps


def test_gaps_keep_commas_inside_one_gap_and_each_gap_once():
    cell = join_gaps(["Experience implementing monitoring, logging, and observability solutions",
                      "container-based deployments", "Container-based deployments"])
    assert split_gaps(cell) == [
        "Experience implementing monitoring, logging, and observability solutions",
        "container-based deployments"]
    assert split_gaps("Java, Go, java") == ["Java", "Go"]  # cells written before 5 Oct


def test_linkedin_card_link_drops_the_tracking_query():
    long = "https://www.linkedin.com/comm/jobs/view/4312345678/?trackingId=abc%3D&refId=x&lipi=y"
    assert short_link(long) == "https://www.linkedin.com/comm/jobs/view/4312345678/"
    assert short_link("https://justjoin.it/job-offer/x?utm=1") == \
        "https://justjoin.it/job-offer/x?utm=1"


def test_a_gap_never_starts_with_and_or_or():
    # Phase 9 P9-12, P9-15: "SLOs, SLIs, and Error Budgets" gave the gap "and Error Budgets".
    from jobengine.screen.models import split_gaps
    assert split_gaps("SLOs, SLIs, and Error Budgets") == ["SLOs", "SLIs", "Error Budgets"]
    assert split_gaps("Helm; or ArgoCD; Android") == ["Helm", "ArgoCD", "Android"]
