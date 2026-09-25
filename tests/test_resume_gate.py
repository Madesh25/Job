import json

import pytest
from resume_helpers import master, plan

from jobengine.llm import FakeLLM
from jobengine.reference import Reference
from jobengine.resume.gate import check, summarise, word_diff
from jobengine.resume.master import render_html
from jobengine.resume.models import Plan
from jobengine.resume.plan import (
    SYSTEM_PROMPT,
    JobContext,
    PlanError,
    make_plan,
    parse_plan,
    user_prompt,
)
from jobengine.settings import ROOT_DIR

REF = Reference.fake()
M = master()
TAILOR = ROOT_DIR / "fixtures" / "llm" / "tailor"
ROWS = [(r.label, list(r.items)) for r in M.skills_main]
ALSO = [(r.label, list(r.items)) for r in M.skills_also]
JOB = JobContext(page_id="p1", company="Vistula Cloud", role="DevOps Engineer",
                 country="Poland", jd="We run EKS with Argo CD.", specific_details=["EKS"])


def fixture_plan(name):
    return parse_plan(json.loads((TAILOR / f"{name}.json").read_text()), M)


def edit(bullet_id, old, new):
    return {"id": bullet_id, "text": M.bullet(bullet_id).text.replace(old, new)}


def errors(**changes):
    return check(M, plan(M, **changes), REF)


def test_unchanged_and_valid_plans_pass():
    assert check(M, Plan.unchanged(M), REF) == []
    assert check(M, fixture_plan("plan_valid"), REF) == []


@pytest.mark.parametrize(
    "name, expected",
    [
        ("plan_unbacked_skill", "'Istio' is not backed"),
        ("plan_over_word_limit", "at most 8 per bullet"),
        ("plan_changed_number", "j1b4: a number changed"),
    ],
)
def test_fixture_plans_are_rejected(name, expected):
    assert any(expected in e for e in check(M, fixture_plan(name), REF))


# ---------------------------------------------------------------- skills: rejected


def test_rejects_known_gap_learning_skill_and_unbacked_item():
    also = ALSO[:-1] + [("Databases", ["PostgreSQL", "Java"])]
    assert any("'Java' is a known gap" in e for e in errors(skills_also=also))
    also = ALSO[:-1] + [("Databases", ["PostgreSQL", "Go"])]
    assert any("'Go' is only a Learning skill" in e for e in errors(skills_also=also))
    also = ALSO[:-1] + [("Databases", ["PostgreSQL", "Pulumi"])]  # Term Map row Needs review
    assert any("'Pulumi' is not backed" in e for e in errors(skills_also=also))


def test_rejects_disallowed_rename_and_too_few_rows():
    renamed = [("Automation", ROWS[4][1])] + ROWS[:4] + ROWS[5:]
    assert any("'Automation' is not an allowed rename of 'CI/CD'" in e
               for e in errors(skills_main=renamed))
    assert any("main table has 4 rows" in e for e in errors(skills_main=ROWS[:4]))
    assert any("needs at least 1 row" in e for e in errors(skills_also=[]))


def test_rejects_duplicates_and_long_dashes():
    dup = ROWS[:-1] + [("Systems & Scripting", ["Linux (RHEL / Ubuntu)", "Bash", "EKS"])]
    assert any("'EKS' appears twice" in e for e in errors(skills_main=dup))
    dash = [edit("j0b2", "end to end", "end " + chr(0x2014) + " end")]
    assert any("em dash or en dash" in e for e in errors(bullet_edits=dash))


# ---------------------------------------------------------------- skills: accepted


def test_accepts_remove_reorder_move_and_rename():
    main = [("CI/CD Pipelines", ["GitLab CI", "Jenkins"])] + ROWS[:4] + ROWS[5:]
    main[1] = ("Kubernetes", ["Helm", "EKS", "Docker", "Argo CD"])  # Argo CD moved up
    also = [("GitOps & Registry", ["Harbor"]), ALSO[0], ("Databases", ["PostgreSQL"])]
    assert errors(skills_main=main, skills_also=also) == []


def test_rename_to_active_term_map_jd_term():
    # Term Map: "EKS, Amazon EKS" is truthfully "Kubernetes on AWS". A row labelled
    # "Kubernetes on AWS" may therefore be called "Amazon EKS".
    master_rows = ROWS[1:]
    m = master()
    m.skills_main[0] = type(m.skills_main[0])(label="Kubernetes on AWS",
                                                items=m.skills_main[0].items)
    p = plan(m, skills_main=[("Amazon EKS", list(m.skills_main[0].items))] + master_rows)
    assert not [e for e in check(m, p, REF) if "label" in e]


# ---------------------------------------------------------------- bullets


def test_accepts_backed_insert_with_connectors():
    ok = [edit("j0b3", "on self-managed clusters", "on self-managed clusters with Argo CD")]
    assert errors(bullet_edits=ok) == []


def test_rejects_unbacked_insert_and_word_limits():
    bad = [edit("j0b3", "on self-managed clusters", "on self-managed clusters with Istio")]
    assert any("'Istio' is not a backed term" in e for e in errors(bullet_edits=bad))
    nine = [edit("j0b3", "with a pipeline", "with a Kubernetes and Terraform and Helm and Docker "
                                            "and Ansible pipeline")]
    assert any("+9 words" in e for e in errors(bullet_edits=nine))
    four = "with Kubernetes and Helm"  # 4 words each
    many = [edit(i, "", "") for i in ()]
    for bullet_id in ("j0b0", "j0b1", "j0b2", "j0b3", "j0b4", "j1b0", "j1b2"):
        text = M.bullet(bullet_id).text.rstrip(".") + f" {four}."
        many.append({"id": bullet_id, "text": text})
    assert any("+28 words in total" in e for e in errors(bullet_edits=many))


def test_rejects_changed_number_and_unknown_id():
    number = [edit("j1b1", "4 scheduled", "6 scheduled")]
    assert any("a number changed" in e for e in errors(bullet_edits=number))
    assert any("no such bullet" in e for e in errors(bullet_edits=[{"id": "j9b9", "text": "x"}]))


def test_word_diff_counts_replacements_as_inserts():
    diff = word_diff("x", "moved to cloud environments", "moved to Kubernetes environments on AWS")
    assert diff.inserted == 3
    assert [c.words for c in diff.chunks] == [("Kubernetes",), ("on", "AWS")]


def test_frozen_comparison_is_part_of_the_gate(monkeypatch):
    from jobengine.resume import gate

    def tampered(master_, plan_, sections=None):
        html = render_html(master_, plan_, sections)
        return html if plan_ == Plan.unchanged(master_) else html.replace("Trainee", "Engineer")

    monkeypatch.setattr(gate, "render_html", tampered)
    assert any("frozen part changed" in e for e in check(M, fixture_plan("plan_valid"), REF))


def test_summary():
    changes = summarise(M, fixture_plan("plan_valid"))
    assert changes.words == 8 and changes.bullets == 3
    assert "-Oracle" in changes.skills and "-MS SQL" in changes.skills
    assert "CI/CD -> CI/CD Pipelines" in changes.skills
    assert "+GitHub Actions" in changes.skills
    assert changes.diff_score == f"+8 words in 3 bullets, {len(changes.skills)} skill changes"
    assert 'j1b1: +2 words "with EKS,"' in changes.lines


# ---------------------------------------------------------------- plan


def test_make_plan_uses_tailor_stage_and_no_contacts(tmp_path):
    (tmp_path / "tailor").mkdir()
    (tmp_path / "tailor" / "p1-r1.json").write_text((TAILOR / "plan_valid.json").read_text())
    llm = FakeLLM(tmp_path)
    p = make_plan(llm, M, JOB, REF, key="p1-r1")
    assert p.priority.bullet_edits[0] == "j0b0"
    call = llm.calls[0]
    assert call["stage"] == "tailor" and call["system"] == SYSTEM_PROMPT
    assert "alex@example.com" not in call["user"] and "555" not in call["user"]
    assert "Argo CD (Hands-on)" in call["user"]
    assert "EKS, Amazon EKS (Term Map: truthfully Kubernetes on AWS)" in call["user"]


def test_correction_and_errors_go_into_the_prompt():
    text = user_prompt(M, JOB, REF, correction="drop Oracle", previous=Plan.unchanged(M),
                       errors=["j0b0: +9 words"])
    assert "correction_from_candidate" in text and "drop Oracle" in text
    assert "gate_errors_to_fix" in text and "previous_plan" in text


def test_parse_plan_defaults_and_errors():
    assert parse_plan({}, M) == Plan.unchanged(M)
    with pytest.raises(PlanError):
        parse_plan({"bullet_edits": [{"id": "j0b0"}]}, M)
