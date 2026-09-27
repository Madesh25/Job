import dataclasses
import json
import os
from types import SimpleNamespace

import pytest

from jobengine.llm import FakeLLM
from jobengine.resume import builder
from jobengine.resume.builder import (
    APPLY_TEXT,
    NEWER_TEXT,
    build_resume,
    fake_deps,
    finalise,
    find_log_by_ref,
    mark_applied,
)
from jobengine.resume.models import Measure
from jobengine.resume.sections import NO_CERT_DATE, PROJECTS_REFUSED, fake_rows
from jobengine.settings import ROOT_DIR, load_settings

JOB = "fixture-clean-pl"
LINKS = ["mailto:alex@example.com", "tel:+15550100000", "https://alex.example.com",
         "https://github.com/alex-example", "https://www.linkedin.com/in/alex-example/"]
TAILOR = ROOT_DIR / "fixtures" / "llm" / "tailor"


class Renders:
    def __init__(self, fill=91.0):
        self.fill = fill
        self.htmls = []

    def render(self, html):
        self.htmls.append(html)
        return b"%PDF-fake " + str(len(self.htmls)).encode()

    def measure(self, html):
        return Measure(pages=1, fill=self.fill, text="", links=LINKS, fonts={"Lato"})


def make(tmp_path, env="local", **environ):
    s = load_settings(env, environ)
    deps = fake_deps(s, out_dir=tmp_path)
    renders = Renders()
    deps.render, deps.measure = renders.render, renders.measure
    return deps, renders


@pytest.fixture
def deps(tmp_path):
    return make(tmp_path)[0]


def test_build_writes_resume_log_row_and_updates_job(deps):
    out = build_resume(deps, JOB)
    assert out.status == "built" and out.revision == 1
    row = deps.resume_log.get(out.log_id)
    assert row["Resume name"] == "Alex_Devops_VistulaCloud.pdf r1"
    assert row["Job"] == [JOB] and row["Revision"] == 1 and row["Approved"] is False
    assert row["Page fill"] == "91.0%"
    assert row["Engine version"].startswith("V16 gm:") and len(row["Engine version"]) == 15
    assert row["Diff score"].startswith("+8 words in 3 bullets, ")
    assert row["Summary focus"] == "unchanged"
    assert row["Evidence used"] == ["ev-eks-migration", "ev-pipelines"]
    body = deps.resume_log.read_body(out.log_id)
    assert body[0] == "Plan" and json.loads(body[1])["skills_main"][0]["label"] == "Kubernetes"
    assert "Changes" in body and "skills: -MS SQL" in body
    assert body[-2:] == ["Gaps reported", "Istio"]
    job = deps.jobs.rows[JOB]
    assert job["Status"] == "Resume built" and job["Resume"] == [out.log_id]


def test_caption(deps):
    out = build_resume(deps, JOB)
    assert out.caption == "\n".join([
        "Resume for Vistula Cloud, DevOps Engineer (version 1)",
        "One page, 91.0% full (the target is 88 to 96%)",
        "",
        "Skills",
        "- Removed: MS SQL",
        "- Added: GitHub Actions",
        "- Renamed: CI/CD -> CI/CD Pipelines",
        "- Moved: GitOps & Registry row to main table; Cloud (GCP) row to Also worked with",
        "",
        "Experience: 8 words added in 3 bullets",
        '- EXAMPLE CORP bullet 1: "cloud" -> "Kubernetes"; added "on AWS,"',
        '- EXAMPLE CORP bullet 4: added "Kubernetes"; added "GitLab CI"',
        '- SAMPLE SYSTEMS bullet 2: added "with EKS,"',
        "",
        "Not in your skills (saved to the job's Gaps, see /gaps): Istio",
        "Ref RL-00000001",
        "Reply to this message to change something, or tap a button.",
    ])
    # The reported gap is saved on the job for /gaps.
    assert "Istio" in deps.jobs.rows[JOB]["Gaps"]


def test_caption_is_cut_to_telegram_limit():
    from types import SimpleNamespace as NS

    from jobengine.resume import gate as g
    from jobengine.resume.builder import caption

    notes = [f"COMPANY bullet {i}: added \"{'x' * 40}\"" for i in range(30)]
    changes = g.Changes(words=40, bullets=30, skills=["-A"], bullet_notes=notes)
    plan = NS(gaps_reported=[], forced_skills=[])
    text = caption(1, {"Company": "C", "Role": "R"}, 90.0, changes, plan, "a" * 32)
    assert len(text) <= 1024
    assert "- ... (full list in Resume Log)" in text
    assert text.endswith("Reply to this message to change something, or tap a button.")


def test_merge_gaps_adds_each_gap_once():
    from jobengine.resume.builder import merge_gaps

    assert merge_gaps("Istio, Go", ["go", "PowerShell"]) == "Istio, Go, PowerShell"
    assert merge_gaps(None, ["PowerShell"]) == "PowerShell"
    assert merge_gaps("", []) == ""


def test_second_approve_shows_existing_build(deps):
    first = build_resume(deps, JOB)
    again = build_resume(deps, JOB)
    assert again.status == "existing" and again.log_id == first.log_id
    assert len(deps.resume_log.rows) == 1


def test_correction_builds_revision_two(deps):
    llm = FakeLLM(default={})
    deps.llm = lambda config: llm
    build_resume(deps, JOB)
    out = build_resume(deps, JOB, correction="drop Oracle", force=True)
    assert out.status == "built" and out.revision == 2
    assert "Oracle" in out.caption.split("- Removed: ")[1].splitlines()[0]
    prompt = llm.calls[-1]["user"]
    assert "drop Oracle" in prompt and "previous_plan" in prompt
    assert deps.jobs.rows[JOB]["Resume"] == [f"{n:08x}-0000-4000-8000-{n:012x}" for n in (1, 2)]


def test_refused_correction_builds_nothing(deps):
    build_resume(deps, JOB)
    build_resume(deps, JOB, force=True)
    out = build_resume(deps, JOB, correction="add Istio", force=True)  # r3 fixture refuses
    assert out.status == "correction_refused"
    assert out.message.startswith("Correction not applied: Istio is not in Skills Inventory")
    assert out.message.endswith("learn it before the interview.")
    assert len(deps.resume_log.rows) == 2


@pytest.mark.parametrize("job, text", [
    ("fixture-skip", "its verdict is Skip"),
    ("fixture-new", "its Status is New"),
    ("missing", "No Job Opportunities row missing"),
])
def test_skip_and_unapproved_jobs_are_refused(deps, job, text):
    out = build_resume(deps, job)
    assert out.status == "refused" and text in out.message
    assert deps.resume_log.rows == {}


def test_gate_failure_retries_then_leaves_out_the_broken_parts(deps, tmp_path):
    (tmp_path / "llm" / "tailor").mkdir(parents=True)
    bad = (TAILOR / "plan_unbacked_skill.json").read_text()
    for key in (f"{JOB}-r1", f"{JOB}-r1-a1", f"{JOB}-r1-a2"):
        (tmp_path / "llm" / "tailor" / f"{key}.json").write_text(bad)
    llm = FakeLLM(tmp_path / "llm")
    deps.llm = lambda config: llm
    out = build_resume(deps, JOB)
    assert len(llm.calls) == 3
    assert "gate_errors_to_fix" in llm.calls[1]["user"]
    # The unbacked skill (Istio) is left out: the master's skills tables are used as they are.
    assert out.status == "built"
    assert "Istio" not in str(out.plan.skills_main + out.plan.skills_also)
    assert any(line.startswith("Left out (broke a rule): skills changes") for line in out.changes)
    assert deps.jobs.rows[JOB]["Status"] == "Resume built"


def test_salvage_drops_only_the_broken_bullet_edits():
    from jobengine.resume.builder import salvage
    from jobengine.resume.models import BulletEdit, Plan, PlanRow

    master = SimpleNamespace()
    plan = Plan(skills_main=[PlanRow(label="A", items=["x"])], skills_also=[],
                bullet_edits=[BulletEdit(id="j0b0", text="bad"), BulletEdit(id="j0b1", text="ok")])
    fixed, remaining, left_out = salvage(
        plan, ["j0b0: inserted word 'Foo' is not a backed term or a connector word"], master,
        lambda p: [])
    assert [e.id for e in fixed.bullet_edits] == ["j0b1"]
    assert fixed.skills_main == plan.skills_main  # skills passed: untouched
    assert remaining == [] and left_out == ["edits to j0b0"]


def test_salvage_drops_all_bullet_edits_when_the_total_is_too_high():
    from jobengine.resume.builder import salvage
    from jobengine.resume.models import BulletEdit, Plan

    plan = Plan(skills_main=[], skills_also=[],
                bullet_edits=[BulletEdit(id="j0b0", text="a"), BulletEdit(id="j0b1", text="b")])
    fixed, remaining, left_out = salvage(
        plan, ["experience: +30 words in total, at most 25"], SimpleNamespace(),
        lambda p: ["experience: +30 words in total, at most 25"] if p.bullet_edits else [])
    assert fixed.bullet_edits == [] and remaining == []
    assert left_out == ["all bullet edits"]


def test_finalise_in_dry_run_writes_out_dir_not_drive(tmp_path):
    deps, renders = make(tmp_path)
    out = build_resume(deps, JOB)
    built_html = renders.htmls[-1]
    result = finalise(deps, out.log_id)
    assert result.status == "approved"
    assert result.message == APPLY_TEXT.format(url="https://jobs.example.com/fixture-clean-pl")
    row = deps.resume_log.get(out.log_id)
    assert row["Approved"] is True
    assert row["File"].startswith("DRY RUN: ")
    assert row["File"].endswith("Alex_Devops_VistulaCloud.pdf")
    assert (tmp_path / "Alex_Devops_VistulaCloud.pdf").exists()
    assert deps.drive().calls == []
    assert renders.htmls[-1] == built_html  # re-rendered from the stored plan


def test_finalise_uploads_when_not_dry_run_and_ticks_only_that_revision(tmp_path):
    deps, _ = make(tmp_path, DRY_RUN="false")
    first = build_resume(deps, JOB)
    second = build_resume(deps, JOB, correction="drop Oracle", force=True)
    old = finalise(deps, first.log_id)
    assert old.status == "newer" and old.message == NEWER_TEXT
    assert old.latest.log_id == second.log_id
    result = finalise(deps, second.log_id)
    assert result.file == "https://drive.example.com/1/Alex_Devops_VistulaCloud.pdf"
    assert deps.resume_log.get(second.log_id)["Approved"] is True
    assert deps.resume_log.get(first.log_id)["Approved"] is False
    assert finalise(deps, second.log_id).status == "approved"  # double tap: same answer
    assert len(deps.drive().calls) == 1


def test_role_in_filename_when_company_already_has_an_approved_resume(deps):
    finalise(deps, build_resume(deps, JOB).log_id)
    out = build_resume(deps, "fixture-second-vistula")
    assert out.filename == "Alex_Devops_VistulaCloud_SiteReliabilityEngineer.pdf"


def test_i_applied_only_from_resume_built(deps):
    assert mark_applied(deps, JOB).startswith("Already handled (Status is Approved)")
    build_resume(deps, JOB)
    text = mark_applied(deps, JOB)
    assert text.startswith("Marked as applied on ")
    job = deps.jobs.rows[JOB]
    assert job["Status"] == "Applied"
    assert job["Applied date"] == job["Last activity date"] == deps.today()
    assert mark_applied(deps, JOB).startswith("Already handled (Status is Applied)")


def test_sections_errors_stop_the_build(deps):
    rows = fake_rows()
    deps.sections = lambda: [dataclasses.replace(r, enabled=True) if r.section == "Certifications"
                             else r for r in rows]
    assert build_resume(deps, JOB).message == NO_CERT_DATE
    deps.sections = lambda: [dataclasses.replace(r, enabled=True) if r.section == "Projects"
                             else r for r in rows]
    assert build_resume(deps, JOB).message == PROJECTS_REFUSED


def test_missing_golden_master(deps):
    deps.blocks = lambda: []
    out = build_resume(deps, JOB)
    assert out.message == "Golden Master not found in Notion. Nothing built."


def test_max_revisions(deps, tmp_path):
    deps.llm = lambda config: FakeLLM(tmp_path, default={})  # every revision: unchanged plan
    for _ in range(5):
        build_resume(deps, JOB, force=True)
    out = build_resume(deps, JOB, force=True)
    assert out.status == "refused" and "already has 5 revisions" in out.message


def test_no_write_saves_preview_only(tmp_path):
    deps, _ = make(tmp_path)
    deps.write = False
    out = build_resume(deps, JOB)
    assert out.status == "built" and out.log_id is None
    assert deps.resume_log.rows == {}
    assert (tmp_path / "Alex_Devops_VistulaCloud_r1.pdf").exists()
    assert deps.jobs.rows[JOB]["Status"] == "Approved"


def test_find_log_by_ref(deps):
    out = build_resume(deps, JOB)
    assert find_log_by_ref(deps, "Resume r1 ...\nRef RL-00000001\nReply") == out.log_id
    assert find_log_by_ref(deps, "no reference here") is None
    assert builder.job_for_log(deps, out.log_id) == JOB


def _can_render():
    from jobengine.resume.render import measure_html

    try:
        m = measure_html("<html><body><p style='font-family: Lato'>x</p></body></html>")
    except Exception:
        return False
    return all("Lato" in f for f in m.fonts)


@pytest.mark.render
@pytest.mark.skipif(not os.environ.get("REQUIRE_RENDER") and not _can_render(),
                    reason="WeasyPrint with Pango and Lato needed")
def test_real_build_and_finalise(tmp_path):
    import io

    import pdfplumber

    deps = fake_deps(load_settings("local", {}), out_dir=tmp_path)
    out = build_resume(deps, JOB)
    assert out.status == "built", out.message
    assert 88 <= out.fill <= 96
    with pdfplumber.open(io.BytesIO(out.pdf)) as pdf:
        assert len(pdf.pages) == 1
        text = pdf.pages[0].extract_text()
    assert "ALEX EXAMPLE" in text and "CI/CD Pipelines" in text
    assert finalise(deps, out.log_id).status == "approved"
    assert (tmp_path / "Alex_Devops_VistulaCloud.pdf").read_bytes()[:4] == b"%PDF"


def test_notion_ids_from_buttons():
    assert builder.notion_id("0123456789abcdef0123456789abcdef") == (
        "01234567-89ab-cdef-0123-456789abcdef")
    assert builder.notion_id("pl-clean") == "pl-clean"


def test_force_skills_marks_only_requested_unbacked_items():
    from jobengine.reference import Reference, Skill
    from jobengine.resume.builder import force_skills
    from jobengine.resume.models import Plan, PlanRow

    master = SimpleNamespace(all_items=lambda: ["Bash"])
    ref = Reference(skills=[Skill("1", "Python", "Production")])
    plan = Plan(skills_main=[PlanRow(label="Scripting", items=["Bash", "Python", "Go"])],
                skills_also=[])
    force_skills(plan, master, ref, "add PowerShell and Go please", ["PowerShell"])
    assert plan.skills_main[0].items == ["Bash", "Python", "Go", "PowerShell"]
    assert plan.forced_skills == ["Go", "PowerShell"]  # Python is backed, Bash is in master


def test_the_llm_cannot_force_a_skill(tmp_path):
    from jobengine.llm import FakeLLM
    from jobengine.resume.plan import make_plan

    llm = FakeLLM(default={"forced_skills": ["Istio"]})
    deps, _ = make(tmp_path)
    ctx = builder._context(deps, JOB, deps.jobs.get_values(JOB))
    job = builder._job_context(JOB, deps.jobs.get_values(JOB), deps.jobs.read_body(JOB))
    assert make_plan(llm, ctx.master, job, ctx.reference, key="x").forced_skills == []
