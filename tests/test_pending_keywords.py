"""PR 8: skill match, missing keywords and "use their word" on the /pending card."""

from test_telegram_bot import Clock, settings

from jobengine.reference import Reference
from jobengine.screen import desk as desk_module
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.sweep.rank import Keywords, Ranker

RANKER = Ranker(Reference.fake())


def test_keywords_from_a_description():
    found = RANKER.keywords(
        "We run Amazon EKS with Terraform and GitHub Actions. Nice to have: Java, Pulumi, "
        "Nomad and Snowflake.")
    assert "terraform" in found.have
    assert {"java", "snowflake", "nomad"} <= set(found.missing)
    # Active Term Map rows only: EKS and GitHub Actions; Pulumi's row is Needs review,
    # and Nomad's row is a gap (no true equivalent).
    assert found.reword == [("Amazon EKS", "Kubernetes on AWS"), ("EKS", "Kubernetes on AWS"),
                            ("GitHub Actions", "CI/CD pipelines in GitLab CI")]
    assert found.share == round(100 * len(found.have) / (len(found.have) + len(found.missing)))


def test_share_is_none_when_no_tool_is_named():
    assert Keywords(have=[], missing=[], reword=[]).share is None
    assert RANKER.keywords("A friendly team in a nice office.").share is None


def test_a_skill_you_have_by_name_needs_no_reword():
    assert RANKER.keywords("Kubernetes and Terraform every day.").reword == []


def make_desk():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.screen()
    return d


def test_pending_card_shows_the_keyword_lines():
    d = make_desk()
    cards = {r.page_id: d.card(r, i, 1).text for i, r in enumerate(d.ranked())}
    clean = cards["pl-clean"]
    assert "Skill match: 100% (6 of 6 tools it names)" in clean
    assert "Use their word: EKS (your Kubernetes on AWS)" in clean
    assert "Missing keywords" not in clean
    tulip = cards["nl-sponsor-yes"]
    assert "Skill match: 75% (3 of 4 tools it names)\nMissing keywords: java" in tulip
    # The new lines sit between the match counts and the screening gaps.
    lines = tulip.splitlines()
    assert lines.index("Missing keywords: java") < lines.index("Gaps: Java services")


def test_long_lists_are_cut(monkeypatch):
    monkeypatch.setattr(desk_module, "MAX_KEYWORDS", 1)
    d = make_desk()
    body = ["Description source: adzuna", "Java, Scala, Kotlin and Snowflake with Terraform. "
            * 20]
    lines = d.keyword_lines(body)
    missing = next(line for line in lines if line.startswith("Missing keywords: "))
    assert missing.endswith(" and 3 more")


def test_no_lines_without_a_description():
    d = make_desk()
    assert d.keyword_lines([]) == []
    assert d.keyword_lines(["Screening (2026-10-01)", "Strong | x | y"]) == []
