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
