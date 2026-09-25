import re
from datetime import date

import pytest

from jobengine.notion_repo import NotionClient
from jobengine.safety import SafetyError, check_config_write
from jobengine.settings import load_settings
from jobengine.strategy import runner
from jobengine.strategy.rules_view import non_negotiables, rules_text
from jobengine.sweep.gate import strategy_gate

TODAY = date(2026, 10, 1)


def deps(env="local", key="update", **kw):
    d = runner.fake_deps(load_settings(env, {}), key=key, **kw)
    d.today = lambda: TODAY
    return d


def rows(d, status=None):
    return [v for _, v in d.repo.all_rows() if status is None or v.get("Status") == status]


def hexes(result):
    return [c.buttons[0][1][3:] for c in result.cards if c.buttons[0][0] == "Adopt"]


def test_update_logs_tips_for_review_and_myths_as_rejected():
    d = deps()
    result = runner.run_update(d)
    testing = rows(d, "Testing")
    assert len(testing) == 3
    rejected = [v for v in rows(d, "Rejected") if "auto-rejected" in (v.get("Notes") or "")]
    reasons = sorted(re.search(r"auto-rejected: (.*)$", v["Notes"]).group(1) for v in rejected)
    assert reasons == ["ats score", "conflicts with V16", "keyword stuffing"]
    first = testing[0]
    assert first["Source"] == "https://careers.example.com/blog/ats-parsing-2026"
    assert first["Date added"] == TODAY
    assert first["Notes"].startswith("countries: All; why: Parsers in common ATS platforms")
    assert "1 without a search source dropped" in result.messages[0]
    assert "1 duplicates dropped" in result.messages[0]  # the referral tip is already adopted
    assert len(result.cards) == 3
    assert result.cards[1].text.startswith("Tip 2/3 (Application, Netherlands)\n")
    assert "Source: https://recruiting.example.org/netherlands-hsm-salary-2026" in \
        result.cards[1].text
    call = d.llm(None).calls[0]
    assert call["search"] and call["max_uses"] == 8
    assert "Inject a missing keyword" in call["user"]  # V16 non-negotiables in the prompt
    assert "Ask a peer engineer for a referral" in call["user"]  # adopted tips


def test_gate_opens_only_after_every_tip_is_decided():
    d = deps()
    result = runner.run_update(d)
    config = d.config()
    blocked_before = strategy_gate(config.__class__.from_values(
        {"last_strategy_update": "2026-08-01", "strategy_refresh_days": "30"}), TODAY, d.state,
        d.s)
    assert blocked_before is not None
    ids = hexes(result)
    assert runner.decide(d, ids[0], adopt=True)[0].endswith("2 left to review.")
    assert d.state.get("last_strategy_update") is None
    runner.decide(d, ids[1], adopt=False)
    assert d.state.get("last_strategy_update") is None
    final = runner.decide(d, ids[2], adopt=True)
    assert "Strategy updated. /fetch is open until 2026-10-31." in final[1]
    assert "Adopted tips are not in V16 yet. Ask Claude to fold them in." in final[1]
    assert d.state.get("last_strategy_update") == {"date": "2026-10-01"}
    stale = config.__class__.from_values(
        {"last_strategy_update": "2026-08-01", "strategy_refresh_days": "30"})
    assert strategy_gate(stale, TODAY, d.state, d.s) is None
    assert [v["Status"] for v in rows(d) if "run " in (v.get("Notes") or "")
            and "auto-rejected" not in v["Notes"]] == ["Adopted", "Rejected", "Adopted"]
    assert runner.decide(d, ids[0], adopt=False) == ["Already decided: Adopted."]


def test_zero_tips_needs_confirm_review():
    d = deps(key="update_empty")
    result = runner.run_update(d)
    assert [label for label, _ in result.cards[0].buttons] == ["Confirm review"]
    assert d.state.get("last_strategy_update") is None
    run_id = result.cards[0].buttons[0][1][3:]
    assert runner.confirm_review(d, run_id)[0].startswith("Strategy updated.")
    assert d.state.get("last_strategy_update") == {"date": "2026-10-01"}


def test_update_with_undecided_tips_resends_them():
    d = deps()
    first = runner.run_update(d)
    runner.decide(d, hexes(first)[0], adopt=True)
    again = runner.run_update(d)
    assert again.messages[0].startswith("You still have 2 to review")
    assert len(again.cards) == 2
    assert len(d.llm(None).calls) == 1  # no second research
    forced = runner.run_update(d, force=True)
    assert len(d.llm(None).calls) == 2 and forced.messages[0].startswith("Research done")


def test_prod_stamps_config_not_bot_state():
    d = deps("prod")
    result = runner.run_update(d)
    for pid in hexes(result):
        runner.decide(d, pid, adopt=False)
    assert d.stamped == [("last_strategy_update", "2026-10-01", TODAY)]
    assert d.state.get("last_strategy_update") is None


def test_config_write_allowlist():
    check_config_write("last_strategy_update")
    check_config_write("credits.hunter")
    with pytest.raises(SafetyError):
        check_config_write("strategy_refresh_days")
    with pytest.raises(SafetyError):
        check_config_write("mail.signature")


def test_note_reply_is_stored():
    d = deps()
    result = runner.run_update(d)
    card = result.cards[0].text
    assert runner.add_note(d, card, "Good, but check the  Polish boards too") == \
        "Note saved on the tip."
    row = next(v for v in rows(d, "Testing") if v["Tip / rule"] in card)
    assert row["Notes"].endswith("\nyour note: Good, but check the Polish boards too")
    assert runner.add_note(d, "no ref here", "x") is None


def test_no_write_changes_nothing():
    d = deps(write=False)
    before = len(rows(d))
    result = runner.run_update(d)
    assert len(rows(d)) == before and d.state.get(runner.RUN_KEY) is None
    assert result.cards  # still shows what it found


def test_protected_pages_are_never_written():
    s = load_settings("prod", {})
    client = NotionClient("fake-token", s, sleep=lambda x: None)
    for page in s.notion_pages.values():
        for method, path in (("PATCH", f"/pages/{page}"),
                             ("PATCH", f"/blocks/{page.replace('-', '')}"),
                             ("POST", f"/blocks/{page}/children")):
            with pytest.raises(SafetyError, match="notion.pages"):
                client.request(method, path, {})


def test_rules_view():
    d = deps()
    blocks = d.v16_blocks()
    rules = non_negotiables(blocks)
    assert len(rules) == 8 and rules[0].startswith("Add a term") and "ninth" not in rules[-1]
    text = rules_text(d.config(), blocks, d.repo.all_rows(), TODAY, d.state, d.s)
    assert text.startswith("V16 rules: https://app.notion.com/p/3e36edc2b0d481348577f84b2c39d066")
    assert "- Exceed one page." in text
    assert "follow-up after 7 days, ghosted after 14 more" in text
    assert "- Ask a peer engineer for a referral before you apply" in text
    assert "/fetch is open until" in text or "/fetch is blocked" in text
