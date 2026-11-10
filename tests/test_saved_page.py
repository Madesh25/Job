"""10 Nov, your request: send a job page saved in the browser instead of pasting its text."""

import io
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage

from jobengine import telegram_bot as tb
from jobengine.screen import saved_page
from jobengine.screen.desk import fake_desk
from jobengine.settings import load_settings
from jobengine.sweep.fakes import FAKE_TODAY

LINKEDIN = "https://www.linkedin.com/jobs/view/4012345678/"
ABOUT = ("Northwind Cloud is hiring a Platform Engineer in Cork. "
         + "You run Kubernetes and Terraform on AWS, build CI/CD pipelines and keep the "
           "platform healthy for the product teams. " * 8)


def linkedin_html(url=LINKEDIN, about=ABOUT):
    return (f"<!DOCTYPE html>\n<!-- saved from url=({len(url):04d}){url} -->\n<html><head>"
            "<title>Platform Engineer | Northwind Cloud | LinkedIn</title></head><body>"
            "<nav>Home My Network Jobs Messaging Notifications</nav>"
            "<div class='jobs-unified-top-card'>Platform Engineer, Northwind Cloud, Cork</div>"
            "<div class='jobs-description__content'><h2>About the job</h2>"
            f"<p>{about}</p></div>"
            "<aside>People also viewed: Senior SRE at Elsewhere, Data Engineer at Other</aside>"
            "<script>var tracking = 'x'.repeat(5000);</script></body></html>")


def mhtml(html, url=LINKEDIN):
    message = EmailMessage()
    message["Snapshot-Content-Location"] = url
    message["Subject"] = "Platform Engineer | Northwind Cloud | LinkedIn"
    message.set_content(html, subtype="html", charset="utf-8")
    return message.as_bytes()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 1, 9, 0)

    def __call__(self):
        return self.now


def desk():
    d = fake_desk(load_settings("local", {}), FAKE_TODAY, now=Clock())
    d.screen()
    return d


def test_the_file_types_and_the_job_link_in_the_file():
    assert saved_page.is_saved_page("Platform Engineer.mhtml")
    assert saved_page.is_saved_page("job.HTM") and not saved_page.is_saved_page("cv.pdf")
    page = saved_page.read("job.html", linkedin_html().encode())
    assert page.url == LINKEDIN
    single = saved_page.read("job.mhtml", mhtml(linkedin_html()))
    assert single.url == LINKEDIN and "jobs-description__content" in single.html
    canonical = ('<html><head><link rel="canonical" href="https://acme.example/jobs/7">'
                 "</head><body></body></html>")
    assert saved_page.page_url(canonical) == "https://acme.example/jobs/7"


def test_only_the_job_description_is_taken_from_a_linkedin_page():
    text = saved_page.description(linkedin_html())
    assert text.startswith("About the job\nNorthwind Cloud is hiring")
    assert "People also viewed" not in text and "My Network" not in text
    assert "repeat" not in text


def test_another_site_gives_its_jobposting_description():
    body = "We build cloud platforms. " * 40
    html = ('<html><head><script type="application/ld+json">{"@type": "JobPosting", '
            f'"title": "DevOps Engineer", "description": "<p>{body}</p>"}}</script></head>'
            "<body><p>Cookie banner</p></body></html>")
    assert saved_page.description(html).startswith("We build cloud platforms.")


def test_the_saved_page_of_the_open_jd_job_is_saved_and_screened():
    d = desk()
    d.jd("https://www.linkedin.com/jobs/view/4012345678")
    replies = d.saved_page_file("Platform Engineer.mhtml",
                                mhtml(linkedin_html(url="https://other.example/x")))
    body = d.repo.rows["li-no-jd"]["body"]
    assert body[0] == "Description source: saved page (full)"
    assert body[1].startswith("About the job\nNorthwind Cloud is hiring")
    assert replies[0].text.startswith("Saved the description (")
    assert d.repo.rows["li-no-jd"]["Screen verdict"] != "Unscreened"
    assert d.captures.get() is None


def test_without_jd_the_job_comes_from_the_link_in_the_page():
    d = desk()
    d.saved_page_file("job.html", linkedin_html().encode())
    assert d.repo.rows["li-no-jd"]["body"][0] == "Description source: saved page (full)"


def test_a_page_of_no_known_job_or_without_a_description_says_what_to_do():
    d = desk()
    which = d.saved_page_file("job.html", linkedin_html(url="https://nowhere.example/1").encode())
    assert "which job is it? Send /jd <the job's link> first" in which[0].text
    d.jd("https://www.linkedin.com/jobs/view/4012345678")
    empty = d.saved_page_file("login.html", b"<html><body>Sign in to LinkedIn</body></html>")
    assert "no job description found in it" in empty[0].text
    assert not d.repo.rows["li-no-jd"].get("body")
    assert d.captures.get() is not None  # the /jd stays open: paste or send it again


def test_an_old_jd_is_not_used_for_the_page():
    d = desk()
    d.jd("https://www.irishjobs.ie/job/99")  # not in Notion: asks for company and role
    d.now.now += timedelta(minutes=21)
    d.saved_page_file("job.html", linkedin_html().encode())
    assert d.repo.rows["li-no-jd"]["body"][0] == "Description source: saved page (full)"


def test_sending_the_saved_page_to_the_bot(monkeypatch, capsys, tmp_path):
    page = tmp_path / "Platform Engineer _ Northwind Cloud _ LinkedIn.mhtml"
    page.write_bytes(mhtml(linkedin_html()))
    monkeypatch.setattr(tb, "get_settings", lambda: load_settings("local", {}))
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"file {page}\n"))
    assert tb.main(["--fake"]) == 0
    out = capsys.readouterr().out
    assert "Saved the description (" in out


def test_a_new_job_link_takes_company_and_role_from_the_page():
    d = desk()
    d.jd("https://jobs.example.com/77")
    d.saved_page_file("job.html", linkedin_html(url="https://jobs.example.com/77").encode())
    created = [r for r in d.repo.rows.values() if r.get("URL") == "https://jobs.example.com/77"]
    assert len(created) == 1 and created[0]["Role"] == "Platform Engineer"
    assert created[0]["body"][0] == "Description source: saved page (full)"
