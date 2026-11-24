"""Close roles kept by the title filter (LOG-07, 24 Nov test log, /fetchreport: CI/CD Engineer,
AI Platform & Deployment Engineer and Cloud Security Engineer were dropped as "title not
DevOps-type"). Screening still judges each job on its description."""

from test_runner import S

from jobengine.sweep.normalize import Rules, title_scope

RULES = Rules.from_config(S.sweep)


def test_the_close_roles_are_in_scope():
    for title in ("CI/CD Engineer", "CI-CD Engineer", "CICD Engineer",
                  "AI Platform & Deployment Engineer", "Cloud Security Engineer",
                  "MLOps Engineer", "Senior Cloud Security Engineer"):
        assert title_scope(title, RULES) is None, title


def test_other_titles_stay_out():
    assert title_scope("Security Engineer", RULES) == "title not in scope"
    assert title_scope("Senior Technical Product Owner", RULES) == "title not in scope"
    assert title_scope("Lead CI/CD Engineer", RULES) == "title excluded (lead)"
    assert title_scope("Cloud Security Architect", RULES) == "title excluded (architect)"
