import json
import os
import re

import pytest
from resume_helpers import CONFIG, master, plan

from jobengine.resume.master import normalised_text, render_html
from jobengine.resume.models import Measure, Plan
from jobengine.resume.plan import parse_plan
from jobengine.resume.render import (
    NO_LATO,
    FitError,
    check_measure,
    filename,
    fit,
    measure_html,
)
from jobengine.resume.sections import fake_rows, resolve
from jobengine.settings import ROOT_DIR

M = master()
PROFILE_LINKS = [
    "mailto:alex@example.com", "tel:+15550100000", "https://alex.example.com",
    "https://github.com/alex-example", "https://www.linkedin.com/in/alex-example/",
]
TAILOR = ROOT_DIR / "fixtures" / "llm" / "tailor"
STYLE_RE = re.compile(r"<style>.*?</style>", re.DOTALL)


def tailor(name):
    return parse_plan(json.loads((TAILOR / f"{name}.json").read_text()), M)


class FakeMeasure:
    """Fill grows with the amount of text: 20 characters are 1 percent."""

    def __init__(self, base_fill=91.0):
        self.base = len(normalised_text(M.html))
        self.base_fill = base_fill
        self.htmls = []

    def __call__(self, html):
        self.htmls.append(html)
        fill = self.base_fill + (len(normalised_text(html)) - self.base) / 20
        pages = 1 if fill <= 100 else 2
        return Measure(pages=pages, fill=round(fill, 1), text="", links=PROFILE_LINKS)


def edit(bullet_id, words):
    text = M.bullet(bullet_id).text.rstrip(".") + " " + words + "."
    return {"id": bullet_id, "text": text}


# ---------------------------------------------------------------- fit (fake measure)


def test_fit_leaves_a_fitting_plan_alone():
    p = tailor("plan_valid")
    result = fit(M, p, None, FakeMeasure())
    assert result.plan == p and result.steps == []


def test_overflow_reverts_least_important_edit_first_and_never_touches_css():
    long = "with Kubernetes and Helm and Docker"
    p = plan(M, bullet_edits=[edit("j0b1", long), edit("j0b2", long), edit("j1b0", long)],
             priority={"bullet_edits": ["j0b2", "j0b1", "j1b0"],
                       "skill_items_low_relevance": []})
    measure = FakeMeasure(base_fill=91.0)
    result = fit(M, p, None, measure, fill_max=96)
    assert result.steps[0] == "overflow: reverted j1b0"
    assert [e.id for e in result.plan.bullet_edits][0] == "j0b1" or result.steps[1:]
    assert result.measure.fill <= 96
    assert all(STYLE_RE.search(h).group(0) == STYLE_RE.search(M.html).group(0)
               for h in measure.htmls)
    assert all(h.count("<li>") == M.html.count("<li>") for h in measure.htmls)  # no bullet lost


def test_overflow_then_removes_low_relevance_items_but_keeps_minimum_rows():
    p = plan(M, priority={"bullet_edits": [],
                          "skill_items_low_relevance": ["MS SQL", "Oracle", "Loki"]})
    result = fit(M, p, None, FakeMeasure(base_fill=96.4), fill_max=96)
    assert result.steps == ["overflow: removed MS SQL"]
    small = plan(M, skills_main=[(r.label, [r.items[0]]) for r in M.skills_main[:5]],
                 priority={"bullet_edits": [],
                           "skill_items_low_relevance": [r.items[0] for r in M.skills_main]})
    with pytest.raises(FitError, match="does not fit"):
        fit(M, small, None, FakeMeasure(base_fill=150), fill_max=96)


def test_underfill_restores_rows_then_items():
    p = plan(M, skills_also=[("Azure", ["VNet"])],
             priority={"bullet_edits": [], "skill_items_low_relevance": []})
    result = fit(M, p, None, FakeMeasure(base_fill=91), fill_min=90.5)
    assert result.steps[0] == "underfill: restored row GitOps & Registry"
    assert result.measure.fill >= 90.5
    with pytest.raises(FitError, match="fills only"):
        fit(M, Plan.unchanged(M), None, FakeMeasure(base_fill=70))


# ---------------------------------------------------------------- checks and filename


def test_check_measure():
    ok = Measure(pages=1, fill=91.0, text="plain - text", links=PROFILE_LINKS, fonts={"Lato"})
    from resume_helpers import CONFIG as config

    from jobengine.resume.master import profile_from_config
    profile = profile_from_config(config)
    assert check_measure(ok, profile, 88, 96) == []
    bad = Measure(pages=2, fill=97.0, text="a " + chr(0x2013) + " b",
                  links=PROFILE_LINKS[:4], fonts={"DejaVuSans"})
    errors = check_measure(bad, profile, 88, 96)
    assert NO_LATO in errors
    assert any("2 pages" in e for e in errors)
    assert any("97.0%" in e for e in errors)
    assert any("em dash" in e for e in errors)
    assert any("header links" in e for e in errors)


def test_filename_rule():
    assert filename(None, "Vistula Cloud Sp. z o.o.") == "Madeshwaran_Devops_VistulaCloudSpzoo.pdf"
    assert filename("Alex_Devops_<Company>.pdf", "Tulip Data B.V.") == "Alex_Devops_TulipDataBV.pdf"
    assert filename(None, "Odra", "Senior Site Reliability Engineer (Platform) and more") == (
        "Madeshwaran_Devops_Odra_SeniorSiteReliabilityEngineerPlatformand.pdf")


# ---------------------------------------------------------------- real rendering


def _can_render():
    try:
        m = measure_html("<html><body><p style='font-family: Lato'>x</p></body></html>")
    except Exception:
        return False
    return all("Lato" in f for f in m.fonts)


# CI sets REQUIRE_RENDER=1 so a missing Pango or Lato fails instead of skipping.
render = pytest.mark.skipif(not os.environ.get("REQUIRE_RENDER") and not _can_render(),
                            reason="WeasyPrint with Pango and Lato needed")


@pytest.mark.render
@render
@pytest.mark.parametrize("country", ["Poland", "Ireland", "Netherlands"])
def test_fake_master_renders_on_one_page_about_90_to_93_percent(country):
    from jobengine.resume.master import profile_from_config

    sections = resolve(fake_rows(), country, CONFIG)
    m = measure_html(render_html(M, Plan.unchanged(M), sections))
    assert check_measure(m, profile_from_config(CONFIG), 88, 96) == []
    assert 90 <= m.fill <= 93
    assert m.links == PROFILE_LINKS


@pytest.mark.render
@render
def test_real_overflow_plan_is_fitted_by_reverting_the_least_important_edit():
    sections = resolve(fake_rows(), "Poland", CONFIG)
    over = tailor("plan_overflow")
    assert measure_html(render_html(M, over, sections)).fill > 96
    result = fit(M, over, sections)
    assert result.steps == ["overflow: reverted j1b3"]
    assert 88 <= result.measure.fill <= 96
    assert STYLE_RE.search(result.html).group(0) == STYLE_RE.search(M.html).group(0)
