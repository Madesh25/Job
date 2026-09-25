import dataclasses

import pytest
from resume_helpers import CONFIG, master

from jobengine.config_store import ConfigStore
from jobengine.resume.master import render_html
from jobengine.resume.models import Plan
from jobengine.resume.sections import (
    NO_CERT_DATE,
    PROJECTS_REFUSED,
    SectionError,
    fake_rows,
    resolve,
)

ROWS = fake_rows()


def with_row(name, **changes):
    return [dataclasses.replace(r, **changes) if r.section == name else r for r in ROWS]


def test_defaults_gdpr_for_poland_only():
    assert resolve(ROWS, "Poland", CONFIG).gdpr.startswith("I, Alex Example, consent")
    assert resolve(ROWS, "Ireland", CONFIG).gdpr is None
    assert resolve(ROWS, "Netherlands", CONFIG).cert_html is None


def test_gdpr_line_is_rendered_last_small_and_italic():
    m = master()
    html = render_html(m, Plan.unchanged(m), resolve(ROWS, "Poland", CONFIG))
    tail = html.split("<h2>Education</h2>")[1]
    assert 'style="font-size: 8pt; font-style: italic; text-align: center;' in tail
    assert tail.rstrip().endswith("</html>")
    plain = render_html(m, Plan.unchanged(m), resolve(ROWS, "Ireland", CONFIG))
    assert plain == m.html


def test_certifications_placeholder_date_fails():
    rows = with_row("Certifications", enabled=True)
    with pytest.raises(SectionError, match=NO_CERT_DATE):
        resolve(rows, "Poland", CONFIG)
    with pytest.raises(SectionError, match=NO_CERT_DATE):
        resolve(with_row("Certifications", enabled=True, content=" "), "Poland", CONFIG)


def test_certifications_with_date_renders_and_shrinks_one_job():
    rows = with_row("Certifications", enabled=True,
                    content="Example Cloud Certificate (ECC), Example Foundation, Jan 2026")
    plan = resolve(rows, "Ireland", CONFIG)
    m = master()
    html = render_html(m, Plan.unchanged(m), plan)
    cert = html.index("<h2>Certifications</h2>")
    assert html.index("<h2>Professional Experience</h2>") < cert < html.index("<h2>Education</h2>")
    assert "<p><b>Example Cloud Certificate (ECC)</b>, Example Foundation, Jan 2026</p>" in html
    demo = html.split("DEMO WORKS")[1].split("</ul>")[0]
    assert demo.count("<li>") == 1
    assert CONFIG.get("resume.ncr_short_bullet") in demo
    assert html.split("SAMPLE SYSTEMS")[1].split("</ul>")[0].count("<li>") == 5


def test_certifications_need_the_config_keys():
    rows = with_row("Certifications", enabled=True, content="ECC, Example Foundation, Jan 2026")
    config = ConfigStore.from_values({"resume.ncr_short_bullet": "x"})
    with pytest.raises(SectionError, match="resume.shrink_job_when_certs"):
        resolve(rows, "Poland", config)


def test_projects_enabled_is_refused():
    with pytest.raises(SectionError, match=PROJECTS_REFUSED):
        resolve(with_row("Projects", enabled=True), "Poland", CONFIG)


def test_languages_line():
    plan = resolve(with_row("Languages", enabled=True), "Ireland", CONFIG)
    m = master()
    html = render_html(m, Plan.unchanged(m), plan)
    assert "<h2>Languages</h2>\n<p>English (Professional) | Examplish (Native)</p>" in html


def test_country_filter():
    rows = with_row("Languages", enabled=True, countries=("Netherlands",))
    assert resolve(rows, "Ireland", CONFIG).languages is None
    assert resolve(rows, "Netherlands", CONFIG).languages
