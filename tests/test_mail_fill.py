import re

import pytest

from jobengine.config_store import ConfigStore
from jobengine.llm import FakeLLM
from jobengine.mail.fill import (
    PLACEHOLDER_RE,
    FillError,
    MailJob,
    choose_template,
    city_for,
    clean_role,
    fill,
    fill_text,
    pick_detail,
    placeholder_values,
)
from jobengine.mail.templates import fake_templates

JOB = MailJob(page_id="fixture-clean-pl", company="Vistula Cloud", role="DevOps Engineer (m/f/d)",
              city="Kraków", country="Poland",
              details=("running GitOps with Argo CD", "moving workloads to EKS"))


def test_each_type_gets_its_template():
    t = fake_templates()
    assert choose_template(t, "Peer engineer", True).template.key == "peer"
    assert choose_template(t, "Recruiter/TA", True).template.key == "recruiter"
    assert choose_template(t, "Hiring", True).template.key == "hiring"


def test_hiring_without_detail_gets_recruiter_template():
    choice = choose_template(fake_templates(), "Hiring", False)
    assert choice.template.key == "recruiter"
    assert "no specific detail" in choice.note


def test_other_type_is_skipped_and_generic_uses_recruiter():
    t = fake_templates()
    assert choose_template(t, "Other", True).template is None
    assert choose_template(t, "Other", True, generic=True).template.key == "recruiter"


def test_role_city_and_permit():
    assert clean_role("DevOps Engineer (m/f/d)") == "DevOps Engineer"
    assert clean_role("SRE (m/w/d) Cloud") == "SRE Cloud"
    assert clean_role("Platform Engineer [f/m/x]") == "Platform Engineer"
    assert clean_role("Cloud Engineer (all genders)") == "Cloud Engineer"
    assert clean_role("Engineer (AWS)") == "Engineer (AWS)"
    assert city_for(MailJob("x", "C", "R", "Remote", "Ireland")) == "Ireland"
    values = placeholder_values(JOB, "Ola Example", ConfigStore.fake(), "moving workloads to EKS")
    assert values["first_name"] == "Ola"
    assert values["permit_word"] == "a Polish work permit"
    assert values["role"] == "DevOps Engineer"


def _template_segments(text):
    return PLACEHOLDER_RE.split(text)[::2]


@pytest.mark.parametrize("key", ["peer", "recruiter", "hiring"])
def test_filled_body_is_template_with_only_placeholders_replaced(key):
    template = fake_templates()[key]
    values = placeholder_values(JOB, "Ola Example", ConfigStore.fake(), "moving workloads to EKS")
    filled = fill(template, values)
    # Rebuild the expected text from the template pieces and the values: no other change.
    names = PLACEHOLDER_RE.findall(template.body)
    pieces = _template_segments(template.body)
    expected = pieces[0] + "".join(values[n] + pieces[i + 1] for i, n in enumerate(names))
    assert filled.body == expected
    pattern = "".join(re.escape(p) + (".+?" if i < len(names) else "")
                      for i, p in enumerate(pieces))
    assert re.fullmatch(pattern, filled.body, re.DOTALL)
    assert not PLACEHOLDER_RE.search(filled.body + filled.subject)


def test_unfilled_placeholder_aborts():
    with pytest.raises(FillError, match="specific_detail"):
        fill(fake_templates()["hiring"], placeholder_values(JOB, "Ola", ConfigStore.fake(), None))
    with pytest.raises(FillError, match="unknown_thing"):
        fill_text("Hi {unknown_thing}", {"first_name": "Ola"})
    config = ConfigStore.from_values({})
    with pytest.raises(FillError, match="permit_word"):
        fill(fake_templates()["peer"], placeholder_values(JOB, "Ola", config, None))


def test_values_are_not_rescanned():
    assert fill_text("{a} {b}", {"a": "{b}", "b": "x"}) == "{b} x"


def test_pick_detail(tmp_path):
    assert pick_detail((), JOB, None) is None
    assert pick_detail(("only one",), JOB, FakeLLM(base=tmp_path)) == "only one"
    llm = FakeLLM()
    assert pick_detail(JOB.details, JOB, llm, key="fixture-clean-pl") == "moving workloads to EKS"
    assert llm.calls[0]["stage"] == "email"
    bad = FakeLLM(base=tmp_path, default={"index": 9})
    assert pick_detail(JOB.details, JOB, bad, key="x") == JOB.details[0]
