"""Remote job boards (23 Oct): Remotive (its documented API answer) and We Work Remotely (its
RSS feed), with made-up jobs. Only remote jobs open in Europe or your countries are kept."""

from dataclasses import replace
from datetime import datetime

from test_runner import TODAY, S

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.sweep.normalize import Rules, Skipped, normalize
from jobengine.sweep.runner import SOURCE_LABELS, fake_deps, run_sweep
from jobengine.sweep.sources import remote_boards as rb

RULES = Rules.from_config(S.sweep)
ACTIVE = ["Poland", "Netherlands", "Ireland"]
NOW = datetime(2026, 10, 1, 9, 0)


def job(n, title, where, posted="2026-10-01T08:00:00"):
    return {"id": n, "url": f"https://remotive.com/remote-jobs/devops/job-{n}", "title": title,
            "company_name": "Acme Cloud", "category": "DevOps / Sysadmin",
            "publication_date": posted, "candidate_required_location": where,
            "salary": "EUR 60k - 70k" if n == 1 else "",
            "description": "<p>Run <b>Kubernetes</b> on AWS with Terraform.</p>"}


REMOTIVE = {"0-legal-notice": "Remotive API Legal Notice", "job-count": 3, "jobs": [
    job(1, "DevOps Engineer", "Europe"), job(2, "Site Reliability Engineer", "USA"),
    job(3, "Platform Engineer", "Worldwide")]}

WWR = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>We Work Remotely: DevOps and Sysadmin Jobs</title>
<item>
  <title>Tulip Data: Cloud Engineer</title>
  <region>Europe Only</region>
  <description>&lt;p&gt;AWS, Terraform and Kubernetes.&lt;/p&gt;</description>
  <pubDate>Thu, 01 Oct 2026 07:00:00 +0000</pubDate>
  <guid>https://weworkremotely.com/remote-jobs/tulip-data-cloud-engineer</guid>
  <link>https://weworkremotely.com/remote-jobs/tulip-data-cloud-engineer</link>
</item>
<item>
  <title>Northwind: DevOps Engineer</title>
  <region>Anywhere in the World</region>
  <pubDate>Thu, 01 Oct 2026 06:00:00 +0000</pubDate>
  <link>https://weworkremotely.com/remote-jobs/northwind-devops-engineer</link>
</item>
</channel></rss>"""


def fakes(asked=None):
    def get_json(url, params):
        (asked if asked is not None else []).append((url, params))
        return REMOTIVE

    return get_json, lambda url: WWR


def test_both_boards_are_read_with_their_links_and_boards():
    asked = []
    get_json, get_text = fakes(asked)
    result = rb.fetch(S, FakeBotState(), get_json, get_text, NOW)
    assert asked == [(rb.REMOTIVE_URL, {"category": "devops"})]
    first = result.postings[0]
    assert (first.board, first.posting_id, first.location_text) == (
        "Remotive", "remotive-1", "Remote, Europe")
    assert first.url == "https://remotive.com/remote-jobs/devops/job-1"  # the link back
    assert " ".join(first.description.split()) == "Run Kubernetes on AWS with Terraform."
    assert first.salary_text == "EUR 60k - 70k (Remotive)" and first.posted_date == TODAY
    wwr = [p for p in result.postings if p.board == "We Work Remotely"]
    assert [(p.company, p.title, p.location_text) for p in wwr] == [
        ("Tulip Data", "Cloud Engineer", "Remote, Europe Only"),
        ("Northwind", "DevOps Engineer", "Remote, Worldwide")]
    assert wwr[0].posting_id == "wwr-tulip-data-cloud-engineer"
    assert wwr[0].description == "AWS, Terraform and Kubernetes." and wwr[0].posted_date == TODAY


def test_only_jobs_open_in_europe_are_kept():
    get_json, get_text = fakes()
    result = rb.fetch(S, FakeBotState(), get_json, get_text, NOW)
    kept = {}
    for posting in result.postings:
        job = normalize(posting, RULES, ACTIVE)
        if not isinstance(job, Skipped):
            kept[posting.title] = (job.country, job.city)
    assert kept == {"DevOps Engineer": ("Other", "Remote"),
                    "Cloud Engineer": ("Other", "Remote")}  # USA and Worldwide left out


def test_worldwide_jobs_are_kept_when_you_say_so():
    cfg = {**S.sweep, "remote_boards": {**S.sweep["remote_boards"], "worldwide": True}}
    s = S.model_copy(update={"sweep": cfg})
    get_json, get_text = fakes()
    places = {p.title: p.location_text for p in rb.fetch(s, None, get_json, get_text, NOW).postings}
    assert places["Platform Engineer"] == "Remote, Europe"
    assert rb.region_text("", False) == "Remote"


def test_remotive_is_asked_at_most_every_six_hours():
    state, asked = FakeBotState(), []
    get_json, get_text = fakes(asked)
    rb.fetch(S, state, get_json, get_text, NOW)
    again = rb.fetch(S, state, get_json, get_text, datetime(2026, 10, 1, 14, 0))
    assert len(asked) == 1
    assert "remotive: asked less than 6 hours ago, skipped this time" in again.notes[0]
    assert {p.board for p in again.postings} == {"We Work Remotely"}  # WWR is still read
    rb.fetch(S, state, get_json, get_text, datetime(2026, 10, 1, 15, 1))
    assert len(asked) == 2


def test_a_failure_or_a_changed_feed_is_a_note():
    def broken(url, params):
        raise http.HttpError("GET failed: HTTP 429", 429)

    result = rb.fetch(S, None, broken, lambda url: "<html>not a feed</html>", NOW)
    assert result.notes == ["remotive: failed (HTTP 429)",
                            "we work remotely: no jobs in the feed (the feed may have changed)"]


def test_the_sweep_reads_the_remote_boards():
    get_json, get_text = fakes()
    deps = replace(fake_deps(S), remote=lambda state=None: rb.fetch(
        S, state, get_json, get_text, NOW))
    state = FakeBotState()
    summary = run_sweep(S, deps, TODAY, state=state, sources=["remote"])
    assert summary.sources["remote"] == 5 and summary.new == 2
    assert state.get(rb.LAST_CALL_KEY) == {"at": "2026-10-01T09:00:00"}
    assert SOURCE_LABELS["remote"] == "Remote job boards"
    assert {"remotive.com", "weworkremotely.com"} <= set(S.allowed_hosts)
