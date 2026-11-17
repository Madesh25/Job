"""Flow feature 8 (9 Oct): /keywords, the tools your best jobs name, against your profile."""

from datetime import UTC, datetime

from test_telegram_bot import desk, talk  # noqa: F401  (desk is a fixture)

from jobengine.config_store import ConfigRow, ConfigStore
from jobengine.screen import keywords_report

JOBS = [(["Kubernetes", "Terraform"], ["Azure"]), (["Kubernetes"], ["Azure", "Go"]),
        (["Kubernetes", "Terraform", "Kubernetes"], [])]


def test_counts_each_job_once_and_marks_the_profile():
    text = keywords_report.report(JOBS, "DevOps Engineer | Kubernetes, AWS")
    lines = text.splitlines()
    assert lines[0] == ("LinkedIn keywords: the 4 tools your 3 best jobs name most (recruiters "
                        "search by these words):")
    assert lines[1] == "1. Kubernetes: 3 jobs (on your profile)"
    assert lines[2] == "2. Azure: 2 jobs (you do not have it: keep it off your profile (/gaps))"
    assert lines[3] == "3. Terraform: 2 jobs (you have it, NOT on your profile: add it)"
    assert lines[4] == "4. Go: 1 job (you do not have it: keep it off your profile (/gaps))"
    assert lines[-1].startswith("Add to your LinkedIn headline or Skills: Terraform.")


def test_no_profile_text_says_how_to_add_it():
    text = keywords_report.report(JOBS, " ")
    assert "1. Kubernetes: 3 jobs (you have it)" in text
    assert "profile.linkedin_headline and profile.linkedin_skills" in text
    assert keywords_report.report([], "x").startswith("No jobs to count yet")


def test_whole_words_only():
    assert keywords_report.on_profile("Go", "Go, Python")
    assert not keywords_report.on_profile("Go", "Google Cloud")


def test_keywords_command(desk):  # noqa: F811
    desk.repo.rows["pl-clean"]["Status"] = "Applied"
    text = talk(desk, "/keywords").sent[0][1]
    assert text.startswith("[LOCAL] LinkedIn keywords: the ")
    assert "kubernetes: 1 job (you have it)" in text
    rows = dict(ConfigStore.fake()._rows)
    rows["profile.linkedin_headline"] = ConfigRow(key="profile.linkedin_headline",
                                                  value="DevOps | Kubernetes")
    desk.deps.config = lambda: ConfigStore(rows)
    text = talk(desk, "/keywords").sent[0][1]
    assert "kubernetes: 1 job (on your profile)" in text
    assert "terraform: 1 job (you have it, NOT on your profile: add it)" in text


def test_first_digest_of_the_month_reminds(desk):  # noqa: F811
    from jobengine.track.digest import run_weekly_digest

    first = run_weekly_digest(desk.track, datetime(2026, 10, 5, 9, tzinfo=UTC))
    later = run_weekly_digest(desk.track, datetime(2026, 10, 12, 9, tzinfo=UTC))
    assert "Monthly: /keywords" in first and "Monthly: /keywords" not in later


def test_names_of_one_term_map_row_count_as_one_entry():
    # 23 Oct (P9-13): "rca" and "root cause analysis" were listed apart.
    from jobengine.reference import TermRow

    rows = [TermRow("t1", ("Incident management", "root cause analysis", "RCA"),
                    "triaging incidents from alert to root cause", "Active"),
            TermRow("t2", ("Helm",), "Helm charts", "Active"),
            TermRow("t3", ("Spinnaker", "Spin"), "(none)", "Needs review")]
    same = keywords_report.aliases(rows)
    assert same == {"incident management": "Incident management / root cause analysis / RCA",
                    "root cause analysis": "Incident management / root cause analysis / RCA",
                    "rca": "Incident management / root cause analysis / RCA"}
    jobs = [(["rca", "root cause analysis", "kubernetes"], []), (["RCA"], [])]
    text = keywords_report.report(jobs, "Kubernetes and RCA", same)
    # One entry, and since 26 Oct with the ways of working, after the tools.
    assert "Incident management / root cause analysis / RCA 2 (you have it)" in text
    entries = [line for line in text.splitlines() if line[:1].isdigit()]
    assert [e.split(":")[0] for e in entries] == ["1. kubernetes"]


def test_ways_of_working_are_listed_after_the_tools():
    """Phase 9 P9-13 (26 Oct): "incident management" is not a tool."""
    jobs = [(["Kubernetes", "incident management", "CI/CD pipelines", "GitLab CI"],
             ["root cause analysis"]),
            (["Kubernetes", "incident management", "Azure DevOps Pipelines"], [])]
    lines = keywords_report.report(jobs, "Kubernetes").splitlines()
    assert lines[0].startswith("LinkedIn keywords: the 3 tools your 2 best jobs name most")
    assert lines[1:4] == ["1. Kubernetes: 2 jobs (on your profile)",
                          "2. Azure DevOps Pipelines: 1 job (you have it, NOT on your profile: "
                          "add it)",
                          "3. GitLab CI: 1 job (you have it, NOT on your profile: add it)"]
    assert lines[5] == ("Ways of working they also name (not tools: good words for your "
                        "LinkedIn About text):")
    assert lines[6] == ("incident management 2 (you have it); CI/CD pipelines 1 (you have it); "
                        "root cause analysis 1 (not yours yet)")
    assert keywords_report.is_practice("Infrastructure as Code")
    assert not keywords_report.is_practice("GCP Monitoring")
