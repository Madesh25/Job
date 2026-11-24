"""Shorter /pending card (S11, 23 Nov, your ask): verdict, company and role, place, the top 3
gaps and one Why line; everything else behind More. Notion Config pending.card = full brings
back the long card. Made-up jobs."""

from test_pending_keywords import make_desk

from jobengine.config_store import ConfigStore
from jobengine.screen import desk as desk_module
from jobengine.screen.models import JobRow


def row(**values):
    return JobRow("p1", "Example Co", "DevOps Engineer", **values)


def test_the_why_line_comes_from_the_screening_table():
    body = ["Screening (V16, 2026-10-01)", "Strong | Kubernetes | EKS", "Strong | Terraform | IaC",
            "Strong | AWS | 3 years", "Transferable | GitLab CI | GitHub Actions",
            "Gap | Java | none", "Verdict: Apply high"]
    assert desk_module.why_line(row(sponsorship="Stated yes"), body) == (
        "Why: strong on Kubernetes, Terraform (+1 more); 1 transferable; sponsorship stated")
    assert desk_module.why_line(row(), ["Screening (V16)", "Gap | Java | none"]) == (
        "Why: no requirement matched strongly")
    assert desk_module.why_line(row(), body, ("Example Co Ireland Ltd", 7)).endswith(
        "; 7 Irish permits issued to it")


def test_only_the_top_3_gaps():
    assert desk_module.gaps_line(row(gaps="Java; Go; Kafka; Spark; Scala")) == (
        "Gaps: Java; Go; Kafka and 2 more")
    assert desk_module.gaps_line(row()) == "Gaps: none"


def test_the_short_card_and_more():
    d = make_desk()
    rows = d.ranked()
    i = next(i for i, r in enumerate(rows) if r.page_id == "pl-clean")
    card = d.card(rows[i], i, len(rows))
    lines = card.text.splitlines()
    assert lines[1:4] == ["Vistula Cloud, DevOps Engineer", "📍 Kraków, Poland", "Gaps: none"]
    assert lines[4].startswith("Why: strong on ")
    assert len(lines) == 6 and lines[5] == "https://jobs.example.com/pl-clean"
    assert [b[0] for b in card.buttons] == ["Approve", "Skip", "More", "Next"]
    more = card.buttons[2][1]
    assert more == f"mo:{i}:pl-clean" and len(more.encode()) <= 64
    [full] = d.tap(more)
    assert "Skill match: 100% (6 of 6 tools it names)" in full.text
    assert "Contract UoP" in full.text and full.buttons[0] == ("Approve", "ap:pl-clean")


def test_a_snippet_only_job_says_so_in_the_why_line():
    d = make_desk()
    d.repo.rows["pl-clean"].update({"body": ["Description source: adzuna", "A short snippet."],
                                    "Gaps": ""})
    r = next(r for r in d.ranked() if r.page_id == "pl-clean")
    text = d.card(r, 0, 1).text
    assert "Why: only a snippet of the description was found" in text and "Gaps:" not in text


def test_config_full_brings_back_the_long_card():
    d = make_desk()
    base = ConfigStore.fake().all()
    d.deps.config = lambda: ConfigStore.from_values({**base, "pending.card": "full"})
    r = next(r for r in d.ranked() if r.page_id == "pl-clean")
    card = d.card(r, 0, 1)
    assert "Match: " in card.text and [b[0] for b in card.buttons] == ["Approve", "Skip", "Next"]
