"""8 Oct: saved jobs that wait for a description get their page read; LinkedIn county names
("Krakowski"); Indeed feedback buttons are not jobs; the probe covers Jobs.ie and
JobsIreland.ie."""

from jobengine.notion_repo import FakeJobsRepo
from jobengine.settings import load_settings
from jobengine.sweep import backfill, probe
from jobengine.sweep.normalize import Rules, detect_location
from jobengine.sweep.sources import gmail_alerts

S = load_settings("local", {})
LONG = " ".join(["Kubernetes, Terraform and AWS every day in our platform team."] * 20)
PAGE = f"<html><head><title>DevOps</title></head><body><main>{LONG}</main></body></html>"


def row(page_id, board, url, verdict="Unscreened", body=None):
    return {"page_id": page_id, "Company": f"Co {page_id}", "Role": "DevOps Engineer",
            "Board": board, "URL": url, "Country": "Poland", "City": "Kraków",
            "Screen verdict": verdict, "body": body or []}


def test_saved_rows_waiting_for_a_description_get_it():
    repo = FakeJobsRepo([
        row("p1", "Pracuj.pl", "https://www.pracuj.pl/praca/x,oferta,1"),
        row("p2", "LinkedIn", "https://www.linkedin.com/jobs/view/2"),
        row("p3", "Adzuna", "https://www.adzuna.pl/details/3", body=["Description source: x"]),
        row("p4", "Pracuj.pl", "https://www.pracuj.pl/praca/y,oferta,4", verdict="Low"),
    ])
    read = []
    filled = backfill.fill_waiting(repo, lambda url: read.append(url) or (url, PAGE), set())
    assert filled == ["Co p1, DevOps Engineer"]
    assert read == ["https://www.pracuj.pl/praca/x,oferta,1"]  # never LinkedIn, never p3/p4
    assert any("Kubernetes" in block for block in repo.rows["p1"]["body"])
    assert repo.rows["p2"]["body"] == []


def test_a_page_with_nothing_or_an_error_leaves_the_row():
    repo = FakeJobsRepo([row("p1", "Pracuj.pl", "https://www.pracuj.pl/praca/x,oferta,1")])

    def broken(url):
        raise RuntimeError("blocked")

    assert backfill.fill_waiting(repo, broken, set()) == []
    assert backfill.fill_waiting(repo, lambda url: (url, "<html>tiny</html>"), set()) == []
    assert backfill.fill_waiting(repo, lambda url: (url, PAGE), set(), max_pages=0) == []
    assert repo.rows["p1"]["body"] == []


def test_linkedin_county_names_are_the_city():
    rules = Rules.from_config(S.sweep)
    assert detect_location("Krakowski (Hybrid)", rules) == ("Poland", "Kraków")
    assert detect_location("Warszawski (Hybrid)", rules) == ("Poland", "Warszawa")


def test_indeed_feedback_buttons_are_not_jobs():
    cfg = S.sweep["gmail"]
    html = ('<a href="https://ie.indeed.com/rc/clk?jk=1">View job</a>'
            '<a href="https://ie.indeed.com/feedback?a=1">This is a bad match</a>'
            '<a href="https://ie.indeed.com/feedback?a=2">Yes</a>')
    message = gmail_alerts.GmailMessage("m", "Indeed <donotreply@match.indeed.com>", "match",
                                        f"<div>{html}</div>")
    assert gmail_alerts.parse_alert(message, cfg) == []


def test_probe_covers_jobs_ie_and_jobsireland():
    assert probe.search_url(probe.SITES["jobsie"], "DevOps Engineer") == \
        "https://www.jobs.ie/jobs/devops-engineer"
    assert probe.search_url(probe.SITES["jobsireland"], "DevOps Engineer") == \
        "https://jobsireland.ie/en-US/browse-jobs?keyword=devops+engineer"
