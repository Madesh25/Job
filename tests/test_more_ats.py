"""Workable, Recruitee, Personio and Teamtailor readers (23 Oct). The answers below have the
shapes their public career-site feeds are documented with, with made-up companies. Field
names vary between accounts, so the readers also take the other names seen."""

from datetime import date

from jobengine.sweep.models import TargetCompany
from jobengine.sweep.sources import ats

ACME = TargetCompany(name="Acme Cloud", careers_url="", ats_platform="Unknown", active=True)


def keep_all(title, location):
    return True


def test_job_board_links_are_recognised():
    assert ats.board_from_url("https://apply.workable.com/acme-cloud/j/AB12CD/") == ats.Board(
        "workable", "acme-cloud")
    assert ats.board_from_url("https://acme.workable.com/") == ats.Board("workable", "acme")
    assert ats.board_from_url("https://tulip.recruitee.com/o/devops-engineer") == ats.Board(
        "recruitee", "tulip")
    assert ats.board_from_url("https://odra.jobs.personio.com/job/12") == ats.Board(
        "personio", "odra", site="com")
    assert ats.board_from_url("https://delft.teamtailor.com/jobs") == ats.Board(
        "teamtailor", "delft")
    assert ats.board_from_url("https://www.workable.com/") is None
    # A careers page that links to its Recruitee board is found too.
    page = '<a href="https://tulip.recruitee.com/">Jobs</a><a href="https://tulip.recruitee.com/o/x">'
    assert ats.board_in_page("https://tulip.example.com/careers", page).ats == "recruitee"
    assert {"workable", "recruitee", "personio", "teamtailor"} <= set(ats.SUPPORTED)


def test_workable_widget():
    asked = []

    def get(url, params):
        asked.append((url, params))
        return {"name": "Acme Cloud", "jobs": [
            {"title": "DevOps Engineer", "shortcode": "AB12CD", "city": "Warsaw",
             "country": "Poland", "telecommuting": False, "published_on": "2026-10-20",
             "url": "https://apply.workable.com/j/AB12CD", "description": "<p>Run AWS.</p>",
             "locations": [{"city": "Warsaw", "region": "Mazowieckie", "country": "Poland"}]},
            {"title": "Platform Engineer", "shortcode": "EF34", "city": "Amsterdam",
             "country": "Netherlands", "telecommuting": True, "created_at": "2026-10-21"}]}

    postings, dropped, _ = ats.workable(ACME, ats.Board("workable", "acme-cloud"), get, keep_all)
    assert asked == [("https://apply.workable.com/api/v1/widget/accounts/acme-cloud",
                      {"details": "true"})]
    first, second = postings
    assert (first.location_text, first.posting_id, first.posted_date, first.description) == (
        "Warsaw, Mazowieckie, Poland", "workable-acme-cloud-AB12CD", date(2026, 10, 20),
        "Run AWS.")
    assert second.location_text == "Amsterdam, Netherlands (Remote)"
    assert second.url == "https://apply.workable.com/acme-cloud/j/EF34/"
    assert dropped == 0


def test_recruitee_offers():
    def get(url, params):
        assert url == "https://tulip.recruitee.com/api/offers/"
        return {"offers": [
            {"id": 7, "title": "Cloud Engineer", "status": "published", "city": "Utrecht",
             "country": "Netherlands", "remote": True, "hybrid": True,
             "careers_url": "https://tulip.recruitee.com/o/cloud-engineer",
             "published_at": "2026-10-19 08:00:00 UTC",
             "description": "<p>Terraform.</p>", "requirements": "<p>3 years</p>"},
            {"id": 8, "title": "Draft", "status": "draft"}]}

    def keep(title, location):
        return "Engineer" in title

    postings, dropped, _ = ats.recruitee(ACME, ats.Board("recruitee", "tulip"), get, keep)
    [job] = postings
    assert job.location_text == "Utrecht, Netherlands"  # remote and hybrid: hybrid wins
    assert (job.posting_id, job.posted_date) == ("recruitee-tulip-7", date(2026, 10, 19))
    assert job.description == "Terraform.\n3 years" and dropped == 0


PERSONIO = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>1234</id>
    <office>Krakow</office>
    <additionalOffices><additionalOffice>Warsaw</additionalOffice></additionalOffices>
    <name>Site Reliability Engineer</name>
    <createdAt>2026-10-18T09:00:00+00:00</createdAt>
    <jobDescriptions>
      <jobDescription><name>Your tasks</name><value><![CDATA[<p>Kubernetes</p>]]></value>
      </jobDescription>
    </jobDescriptions>
  </position>
</workzag-jobs>"""


def test_personio_feed():
    asked = []

    def page(url):
        asked.append(url)
        return url, PERSONIO

    postings, _, _ = ats.personio(ACME, ats.Board("personio", "odra", site="com"), page,
                                  keep_all)
    [job] = postings
    assert asked == ["https://odra.jobs.personio.com/xml?language=en"]
    assert (job.title, job.location_text, job.posted_date) == (
        "Site Reliability Engineer", "Krakow / Warsaw", date(2026, 10, 18))
    assert job.url == "https://odra.jobs.personio.com/job/1234"
    assert job.description == "Your tasks\nKubernetes"
    assert ats.personio(ACME, ats.Board("personio", "odra"), lambda u: (u, "<html>"),
                        keep_all) == ([], 0, 0)


TEAMTAILOR = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel>
<item>
  <title>DevOps Engineer</title>
  <description>&lt;p&gt;GitLab CI&lt;/p&gt;</description>
  <pubDate>Fri, 16 Oct 2026 10:00:00 +0200</pubDate>
  <link>https://delft.teamtailor.com/jobs/5551-devops-engineer</link>
  <guid>https://delft.teamtailor.com/jobs/5551-devops-engineer</guid>
  <remoteStatus>hybrid</remoteStatus>
  <tt:locations><tt:location><tt:city>Dublin</tt:city><tt:country>Ireland</tt:country>
  </tt:location></tt:locations>
</item>
</channel></rss>"""


def test_teamtailor_feed():
    postings, _, _ = ats.teamtailor(ACME, ats.Board("teamtailor", "delft"),
                                    lambda url: (url, TEAMTAILOR), keep_all)
    [job] = postings
    assert (job.location_text, job.posting_id, job.posted_date) == (
        "Dublin, Ireland", "teamtailor-delft-5551-devops-engineer", date(2026, 10, 16))
    assert job.description == "GitLab CI"


def test_a_board_found_by_name_needs_jobs():
    assert ats._has_jobs({"offers": [{"id": 1}]}) and not ats._has_jobs({"offers": []})
    assert ats._has_jobs({"name": "x", "jobs": [{"title": "y"}]})


# ---------------------------------------------------------------- Eightfold (C4, 26 Oct)


def test_eightfold_links_are_recognised():
    assert ats.board_from_url("https://jobs.ericsson.com/careers?domain=ericsson.com") == \
        ats.Board("eightfold", "ericsson.com", host="jobs.ericsson.com")
    assert ats.board_from_url(
        "https://jobs.ericsson.com/careers/job/563121?domain=ericsson.com&hl=en") == \
        ats.Board("eightfold", "ericsson.com", host="jobs.ericsson.com")
    assert ats.board_from_url("https://apply.careers.microsoft.com/careers/job/1") == \
        ats.Board("eightfold", "microsoft.com", host="apply.careers.microsoft.com")
    board = ats.Board("eightfold", "ericsson.com", host="jobs.ericsson.com")
    assert ats.board_from_url(ats.eightfold_link(board)) == board
    assert "eightfold" in ats.SUPPORTED


def test_eightfold_search_reads_either_answer_shape():
    from jobengine import http

    asked = []

    def get(url, params):
        asked.append((url, params["query"], params["location"]))
        if url.endswith("/api/apply/v2/jobs"):
            raise http.HttpError("GET jobs.example failed: HTTP 404", status=404)
        if params["location"] != "Ireland":
            return {"data": {"positions": []}}
        return {"data": {"positions": [
            {"id": 77, "name": "Site Reliability Engineer", "locations": ["Dublin, Ireland"],
             "postedTs": 1761400000, "positionUrl": "/careers/job/77"},
            {"id": 78, "name": "Sales Manager", "locations": ["Dublin, Ireland"]}]}}

    def keep(title, location):
        return "Engineer" in title

    board = ats.Board("eightfold", "acme.com", host="jobs.acme.example")
    postings, dropped, _ = ats.eightfold(ACME, board, get, keep, ("sre", "devops"))
    [job] = postings
    assert (job.title, job.location_text) == ("Site Reliability Engineer", "Dublin, Ireland")
    assert job.url == "https://jobs.acme.example/careers/job/77"
    assert job.posting_id == "eightfold-acme.com-77"
    assert job.posted_date == date(2025, 10, 25)
    assert dropped == 2  # the sales job, once per search word
    assert asked[0] == ("https://jobs.acme.example/api/apply/v2/jobs", "sre", "Poland")
    assert asked[1] == ("https://jobs.acme.example/api/pcsx/search", "sre", "Poland")
    assert all(url.endswith("/api/pcsx/search") for url, _, _ in asked[2:])
    assert len(asked) == 1 + 2 * 3  # one probe, then 2 words x 3 countries


def test_eightfold_boards_wait_for_the_check_before_fetch_reads_them():
    from jobengine.settings import load_settings

    s = load_settings("local", {})
    boards = s.sweep["ats_boards"]
    assert boards["Microsoft (IE)"]["ats"] == "eightfold"
    assert boards["Ericsson (IE)"]["host"] == "jobs.ericsson.com"
    assert (s.sweep.get("ats") or {}).get("eightfold_in_fetch") is False
    company = TargetCompany("Ericsson (IE)", "https://www.ericsson.com/en/careers", "Unknown",
                            True)

    def no_call(url, params):
        raise AssertionError("an Eightfold board was read by /fetch")

    result = ats.fetch(s, [company], keep_all, get=no_call)
    assert result.own_site == ["Ericsson (IE)"] and result.postings == []
