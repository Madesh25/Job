import json

import yaml

from jobengine.settings import CONFIG_DIR, ROOT_DIR
from jobengine.strategy.models import Tip
from jobengine.strategy.validate import (
    CONFLICT_REASON,
    check,
    is_duplicate,
    matches_pattern,
    norm_url,
    parse_tips,
    reject_reason,
    verified_sources,
)

FIXTURE = json.loads((ROOT_DIR / "fixtures/llm/strategy/update.json").read_text())
URLS = FIXTURE["search_urls"]
PATTERNS = yaml.safe_load((CONFIG_DIR / "base.yaml").read_text())["strategy"]["reject_patterns"]
SEED = [r["Tip / rule"] for r in json.loads(
    (ROOT_DIR / "fixtures/strategy/strategy_seed.json").read_text())]


def tips():
    return parse_tips(FIXTURE["reply"], 20)


def test_parse_tips_limits_and_cleans():
    assert len(parse_tips(FIXTURE["reply"], 3)) == 3
    assert Tip.from_raw({"tip": "  "}) is None
    t = Tip.from_raw({"tip": "x", "category": "Nonsense", "countries": "Poland",
                      "sources": "https://a.example.com"})
    assert (t.category, t.countries, t.sources) == ("Other", ["Poland"], ["https://a.example.com"])


def test_sources_must_come_from_the_search_results():
    assert norm_url("https://www.Example.org/a/") == norm_url("http://example.org/a")
    assert norm_url("not a url") == ""
    tip = Tip("x", sources=["https://recruiting.example.org/netherlands-hsm-salary-2026/",
                            "https://elsewhere.example.net/"])
    assert verified_sources(tip, URLS) == [URLS[1]]
    assert verified_sources(Tip("x", sources=["https://unknown.example.net/x"]), URLS) == []


def test_reject_patterns_and_v16_conflicts():
    assert matches_pattern("Use an ATS Score checker", "ats score")
    assert matches_pattern("Paste the entire job description", PATTERNS[6])
    assert matches_pattern("It is fine to exaggerate your experience", PATTERNS[8])
    assert not matches_pattern("Use standard headings", "ats score")
    assert reject_reason(Tip("Keyword stuffing works"), PATTERNS) == "keyword stuffing"
    assert reject_reason(Tip("Add an appendix", conflicts_with_v16=True), PATTERNS) == \
        CONFLICT_REASON
    assert reject_reason(Tip("Use standard section headings"), PATTERNS) is None


def test_duplicates():
    assert is_duplicate("Ask a peer engineer for a referral before applying through the portal.",
                        SEED)
    assert not is_duplicate("Expect a live Kubernetes task in interviews.", SEED)


def test_check_the_fixture_batch():
    result = check(tips(), URLS, PATTERNS, SEED)
    assert [t.category for t in result.kept] == ["ATS", "Application", "Interview"]
    assert result.kept[1].sources == [URLS[1]]  # normalised to the search result URL
    reasons = {t.tip[:30]: r for t, r in result.rejected}
    assert reasons["Run your resume through an ATS"] == "ats score"
    assert reasons["Add every keyword from the posti"[:30]] == "keyword stuffing"
    assert reasons["Add a two-page project appendix"[:30]] == CONFLICT_REASON
    assert result.no_source == 1  # the follow-up tip cites a page that was not searched
    assert result.duplicates == 2  # the referral tip (seed) and the reworded headings tip
