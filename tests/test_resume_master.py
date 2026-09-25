import pytest
from resume_helpers import CONFIG, master, plan

from jobengine.resume.master import (
    MasterError,
    blank_zones,
    code_blocks,
    fake_blocks,
    find_master,
    frozen_diff,
    normalised_text,
    parse_master,
    profile_from_config,
    render_html,
)
from jobengine.resume.models import Plan


def test_master_comes_from_the_code_block_split_in_pieces():
    blocks = fake_blocks()
    raw, cert = find_master(blocks)
    assert raw.startswith("<!DOCTYPE html>")
    assert raw.rstrip().endswith("</html>")
    assert len(blocks[2]["code"]["rich_text"]) > 1  # really split in 2000-character pieces
    assert cert.startswith("<h2>Certifications</h2>")
    assert len(code_blocks(blocks)) == 3


def test_missing_master_is_an_error():
    with pytest.raises(MasterError, match="Golden Master not found in Notion. Nothing built."):
        find_master([b for b in fake_blocks() if b["type"] != "code"])


def test_parse_skills_and_bullets():
    m = master()
    assert [r.label for r in m.skills_main][:2] == ["Kubernetes", "Cloud (AWS)"]
    assert len(m.skills_main) == 7 and len(m.skills_also) == 3
    assert m.skills_main[-1].label == "Systems & Scripting"
    assert m.skills_main[-1].items == ("Linux (RHEL / Ubuntu)", "Windows Server", "Bash", "Python")
    assert m.companies == ["EXAMPLE CORP", "SAMPLE SYSTEMS", "DEMO WORKS"]
    assert [b.id for b in m.bullets if b.job == 2] == ["j2b0", "j2b1"]
    assert m.bullet("j1b1").text.startswith("Met recovery targets")
    assert len(m.version) == 8


def test_profile_values_replace_the_snapshot():
    m = master()
    assert "snapshot" not in m.html
    assert '<a href="mailto:alex@example.com">alex@example.com</a>' in m.html
    assert '<a href="tel:+15550100000">+1 555 010 0000</a>' in m.html
    assert '<a href="https://alex.example.com">Portfolio</a>' in m.html
    assert '<p class="reloc">Open to relocation within Europe</p>' in m.html
    assert profile_from_config(CONFIG).links()[1] == "tel:+15550100000"


def test_round_trip_unchanged_plan_is_the_master():
    m = master()
    out = render_html(m, Plan.unchanged(m))
    assert normalised_text(out) == normalised_text(m.html)
    assert out == m.html  # byte-identical, not only the same text


def test_render_applies_plan():
    m = master()
    p = plan(m, bullet_edits=[{"id": "j0b1", "text": "Ran a mixed workload with Argo CD."}],
             skills_main=[("CI/CD Pipelines", ["GitLab CI", "Jenkins"])] + [
                 (r.label, list(r.items)) for r in m.skills_main[:4]])
    out = render_html(m, p)
    assert "<li>Ran a mixed workload with Argo CD.</li>" in out
    assert '<td class="k">CI/CD Pipelines</td><td>GitLab CI, Jenkins</td>' in out
    assert "Systems &amp; Scripting" not in out.split("Also worked with")[0].split("Skills")[-1]


def test_frozen_comparison_catches_changed_date_or_title():
    m = master()
    assert frozen_diff(m.html, render_html(m, Plan.unchanged(m))) == []
    changed = m.html.replace("Mar 2024 - Present", "Mar 2023 - Present")
    assert frozen_diff(m.html, changed)[0].startswith("frozen part changed")
    retitled = m.html.replace("Junior Platform Consultant", "Lead Platform Consultant")
    assert frozen_diff(m.html, retitled)
    # Skills and bullet text are not part of the frozen comparison.
    edited = m.html.replace("Terraform, Ansible</td>", "Ansible, Terraform</td>")
    assert frozen_diff(m.html, edited) == []
    assert "<li></li>" in blank_zones(m.html)


def test_structure_errors():
    with pytest.raises(MasterError, match="EDITABLE ZONE 1"):
        parse_master(master().html.replace("EDITABLE ZONE 1 START", "ZONE"),
                     profile_from_config(CONFIG))
