"""Resume match check before approving (S8, 23 Nov, your ask): the resume preview says how
many of the job's key skills the resume names, with a warning below 60%."""

from test_telegram_bot import desk  # noqa: F401

from jobengine.apply import cover

RESUME = "DevOps Engineer. Kubernetes, Terraform and AWS in production; Jenkins pipelines."


def test_the_share_of_the_jobs_key_skills_the_resume_names():
    terms = ["Kubernetes", "Terraform", "AWS", "Kafka", "Go"]
    assert cover.ats_line(terms, RESUME) == ("ATS check: the resume names 3 of the 5 tools the "
                                             "job names (60%).")
    assert cover.ats_line([*terms, "Rust", "Kubernetes"], RESUME) == (
        "⚠️ ATS check: the resume names 3 of the 6 tools the job names (50%), below 60%: "
        "screening software may filter it out.")
    assert cover.ats_line(terms, RESUME, minimum=70).startswith(
        "⚠️ ATS check: the resume names 3 of the 5")
    assert cover.ats_line([], RESUME) is None and cover.ats_line(terms, "") is None


def test_it_says_how_many_more_are_skills_you_have():
    """LOG-06 (24 Nov): the card said Skill match 83% (15 of 18), the preview ATS 44% (8 of
    18): two counts of the same 18 tools; the ATS line now says how they differ."""
    terms = ["Kubernetes", "Terraform", "AWS", "Kafka", "Go"]
    line = cover.ats_line(terms, RESUME, yours=["Kubernetes", "Terraform", "AWS", "Kafka"])
    assert line == ("ATS check: the resume names 3 of the 5 tools the job names (60%); 1 more "
                    "are skills you have (the card's Skill match): Add keywords puts them in.")
    assert "more are skills" not in cover.ats_line(terms, RESUME, yours=["Kubernetes"])


def test_the_preview_shows_the_ats_check_first(desk, monkeypatch):  # noqa: F811
    monkeypatch.setattr(type(desk), "resume_match",
                        lambda self, outcome: ("Resume match: shows 5 of 8 job keywords you "
                                               "have.", []))
    monkeypatch.setattr(type(desk), "resume_ats", lambda self, outcome: (
        "⚠️ ATS check: the resume names 5 of the 10 tools the job names (50%), below 60%: "
        "screening software may filter it out."))
    shown = next(r for r in desk.tap("ap:pl-clean") if r.document)
    lines = shown.text.splitlines()
    assert lines[1].startswith("⚠️ ATS check: the resume names 5 of the 10")
    assert lines[2].startswith("Resume match: shows 5 of 8")


def test_resume_ats_on_a_build_with_the_jobs_description(desk, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cover, "pdf_text", lambda pdf: RESUME)
    shown = next(r for r in desk.tap("ap:pl-clean") if r.document)
    line = next(x for x in shown.text.splitlines() if "ATS check: the resume names " in x)
    assert "tools the job names (" in line
    desk.deps.config = lambda: {"resume.ats_min": "101"}
    assert desk.resume_ats(type("O", (), {"pdf": b"x", "job_id": "pl-clean"})()).startswith(
        "⚠️ ATS check")
