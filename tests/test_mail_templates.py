import copy
import json

from jobengine.mail.templates import (
    FIXTURES,
    approved,
    fake_templates,
    parse_templates,
    template_key,
    unescape,
)


def blocks():
    return json.loads((FIXTURES / "cold_mail_templates_page.json").read_text(encoding="utf-8"))


def test_parses_the_four_templates():
    t = fake_templates()
    assert set(t) == {"peer", "recruiter", "hiring", "followup"}
    assert t["peer"].approved and t["recruiter"].approved and t["hiring"].approved
    assert t["peer"].subject == "{role} role at {company} {country}"
    assert t["hiring"].subject == "{role} on your team at {company}"
    assert t["peer"].body.startswith("Hi {first_name},\n\n")
    assert "{specific_detail}" in t["hiring"].body


def test_section_without_approved_is_never_used():
    t = fake_templates()
    assert t["followup"].approved is False
    assert approved(t, "followup") is None
    changed = blocks()
    for block in changed:
        if block["type"] == "heading_1" and "Hiring" in block["heading_1"]["rich_text"][0][
                "plain_text"]:
            text = "3. Hiring manager : DRAFT"
            block["heading_1"]["rich_text"][0]["plain_text"] = text
            block["heading_1"]["rich_text"][0]["text"]["content"] = text
    assert approved(parse_templates(changed), "hiring") is None


def test_approved_section_wins_over_unapproved_duplicate():
    changed = blocks()
    extra = copy.deepcopy([b for b in changed if b["type"] == "heading_1"][0])
    extra["heading_1"]["rich_text"][0]["plain_text"] = "1b. Peer engineer (new wording) : DRAFT"
    changed.insert(0, extra)
    t = parse_templates(changed)
    assert t["peer"].approved


def test_unescape_and_keys():
    assert unescape("\\{role\\} at \\{company\\}") == "{role} at {company}"
    assert template_key("3. Hiring manager : APPROVED 24 Sep 2026") == "hiring"
    assert template_key("2. Recruiter / Talent Acquisition : APPROVED") == "recruiter"
    assert template_key("Rules the bot must follow") is None


def test_missing_subject_or_body_is_not_usable():
    changed = [b for b in blocks() if not (b["type"] == "code" and "{specific_detail}" in
                                            b["code"]["rich_text"][0]["plain_text"])]
    assert approved(parse_templates(changed), "hiring") is None
