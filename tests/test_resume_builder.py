import dataclasses
import json
import os

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
    lines = out.caption.splitlines()
    assert lines[0] == "Resume r1 for Vistula Cloud, DevOps Engineer"
    assert lines[1].startswith("Fill 91.0% | +8 words in 3 bullets | skills: -MS SQL")
    assert "CI/CD -> CI/CD Pipelines" in lines[1]
    assert lines[2] == "Gaps not added: Istio"
    assert lines[3] == "Ref RL-00000001"
    assert lines[4] == "Reply to this message with a correction, or tap a button."
    assert len(out.caption) <= 1024


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
    assert "-Oracle" in out.caption
    prompt = llm.calls[-1]["user"]
    assert "drop Oracle" in prompt and "previous_plan" in prompt
    assert deps.jobs.rows[JOB]["Resume"] == [f"{n:08x}-0000-4000-8000-{n:012x}" for n in (1, 2)]


def test_refused_correction_builds_nothing(deps):
    build_resume(deps, JOB)
    build_resume(deps, JOB, force=True)
    out = build_resume(deps, JOB, correction="add Istio", force=True)  # r3 fixture refuses
    assert out.status == "correction_refused"
    assert out.message.startswith("Correction not applied: Istio is not in Skills Inventory")
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


def test_gate_failure_retries_then_reports(deps, tmp_path):
    (tmp_path / "llm" / "tailor").mkdir(parents=True)
    bad = (TAILOR / "plan_unbacked_skill.json").read_text()
    for key in (f"{JOB}-r1", f"{JOB}-r1-a1", f"{JOB}-r1-a2"):
        (tmp_path / "llm" / "tailor" / f"{key}.json").write_text(bad)
    llm = FakeLLM(tmp_path / "llm")
    deps.llm = lambda config: llm
    out = build_resume(deps, JOB)
    assert out.status == "failed"
    assert out.message.startswith("Resume could not be built within the rules: skills: 'Istio'")
    assert out.message.endswith("The job stays Approved; tap Rebuild or reply with a correction.")
    assert len(llm.calls) == 3
    assert "gate_errors_to_fix" in llm.calls[1]["user"]
    assert deps.jobs.rows[JOB]["Status"] == "Approved"
    assert deps.resume_log.rows == {}


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
