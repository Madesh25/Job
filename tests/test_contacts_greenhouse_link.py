"""6-01 (10 Nov): /contacts with a Greenhouse job link asked for the company and country and
saved the company's board page with the role "Manychat"."""

import json

from jobengine.screen import add_link

LINK = "https://job-boards.greenhouse.io/manychat/jobs/8077934002"
API = {"title": "Senior Site Reliability Engineer",
       "location": {"name": "Amsterdam, Netherlands"}, "company_name": "Manychat",
       "content": "&lt;p&gt;" + "You keep our Kubernetes platform reliable. " * 20
                  + "&lt;/p&gt;"}


def test_a_greenhouse_job_link_is_read_through_its_api():
    assert add_link.greenhouse_api(LINK) == (
        "https://boards-api.greenhouse.io/v1/boards/manychat/jobs/8077934002")
    assert add_link.greenhouse_api("https://boards.greenhouse.io/acme/jobs/12?gh_src=x") == (
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/12")
    assert add_link.greenhouse_api(
        "https://job-boards.greenhouse.io/embed/job_board?for=manychat") is None
    assert add_link.greenhouse_api("https://acme.com/jobs/1") is None
    job = add_link.from_greenhouse(LINK, json.dumps(API))
    assert (job.title, job.company, job.location) == (
        "Senior Site Reliability Engineer", "Manychat", "Amsterdam, Netherlands")
    assert job.description.startswith("You keep our Kubernetes platform reliable.")
    assert job.url == LINK
    assert add_link.from_greenhouse(LINK, "not json") is None


def test_contacts_saves_the_greenhouse_job_with_its_role_place_and_link():
    from test_review_one_at_a_time import make_desk

    d = make_desk()
    d.say = lambda reply: None
    d.get_json_page = lambda url: (url, json.dumps(API))

    def no_page(url):
        raise AssertionError("the job page itself is not read")

    d.get_slow_page = no_page
    page_id, replies = d._save_job_for_contacts(LINK, "")
    row = d.repo.rows[page_id]
    assert (row["Company"], row["Role"], row["Country"], row["City"], row["URL"]) == (
        "Manychat", "Senior Site Reliability Engineer", "Netherlands", "Amsterdam", LINK)
    assert replies[0].text.startswith("Saved Manychat, Senior Site Reliability Engineer "
                                      "(Amsterdam, Netherlands) as a new job")


CLOSED = "https://job-boards.greenhouse.io/learnupon/jobs/7929249002"


def _closed_api(url):
    from jobengine import http

    raise http.HttpError("GET boards-api.greenhouse.io failed: HTTP 404", 404,
                         "boards-api.greenhouse.io")


def test_a_closed_greenhouse_job_saves_nothing():
    """6-01 (11 Nov): a closed job saved a row "Greenhouse, LearnUpon" with the board page
    (error=true) as its link and greenhouse.io as the company's domain."""
    from test_review_one_at_a_time import make_desk

    d = make_desk()
    d.say = lambda reply: None
    before = len(d.repo.rows)
    d.get_json_page = _closed_api

    def no_page(url):
        raise AssertionError("a closed job's page is not read")

    d.get_slow_page = no_page
    page_id, replies = d._save_job_for_contacts(CLOSED, "")
    assert page_id is None and len(d.repo.rows) == before
    assert replies[0].text.startswith("This job is closed")
    [reply] = d.add_command(CLOSED)
    assert reply.text.startswith("This job is closed") and len(d.repo.rows) == before


def test_the_board_page_with_error_true_means_closed():
    from test_review_one_at_a_time import make_desk

    assert add_link.closed_board_page("https://job-boards.greenhouse.io/learnupon?error=true")
    assert not add_link.closed_board_page("https://job-boards.greenhouse.io/learnupon")
    assert not add_link.closed_board_page("https://acme.com/jobs?error=true")
    d = make_desk()
    d.say = lambda reply: None
    before = len(d.repo.rows)

    def api_down(url):
        from jobengine import http

        raise http.HttpError("GET boards-api.greenhouse.io failed: ReadTimeout")

    d.get_json_page = api_down
    d.get_slow_page = lambda url: ("https://job-boards.greenhouse.io/learnupon?error=true",
                                   "<html><body>Current openings at LearnUpon</body></html>")
    page_id, replies = d._save_job_for_contacts(CLOSED, "")
    assert page_id is None and len(d.repo.rows) == before
    assert replies[0].text.startswith("This job is closed")
