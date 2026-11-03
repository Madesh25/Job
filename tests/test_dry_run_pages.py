"""Phase 19 (3 Nov): /dryrun on a job's description page filled the site's own job search
and hidden location boxes ("Filled 0 of 10"), and an email-first sign-in page was not seen
as a login. Now the site's search, menus and hidden fields are not read, the job page's
Apply link is opened (a plain GET, nothing pressed), and a page with no application field
stops with the reason. Made-up pages."""

from test_form_bot import FORM, SAVED, VALUES, browser
from test_telegram_bot import settings

from jobengine.apply import form_bot
from jobengine.apply.form_bot import Field

JOB_PAGE = """<!doctype html><html><head><title>Systems Engineer</title></head><body>
<header><form action="/en/search"><input aria-label="Search for jobs by title or keyword"
  name="base_query"><input aria-label="search job by location" name="loc_query"></form></header>
<form action="/en/search"><input name="city" aria-label="city" class="invisible"
  style="visibility:hidden"><input name="country" aria-label="country" style="opacity:0"></form>
<h1>Systems Engineer</h1><a href="https://jobs.example.com/apply">Apply now</a>
<p>Build and run the platform.</p></body></html>"""
LOGIN = """<!doctype html><html><head><title>Amazon.jobs</title></head><body>
<h1>Log in or create account</h1><label for="e">Email</label><input id="e" type="email">
<button>Continue</button></body></html>"""
SEARCH_ONLY = """<!doctype html><html><head><title>Jobs</title></head><body>
<input aria-label="Keyword"><select aria-label="Country"><option>Ireland</option></select>
</body></html>"""


def _page(html):
    return lambda route: route.fulfill(status=200, body=html, content_type="text/html")


def serve(pages):
    def prepare(context):
        for url, html in pages.items():
            context.route(url, _page(html))
    return prepare


def test_what_counts_as_an_application_form():
    assert form_bot.application_form([Field("1", "text", "First name")])
    assert form_bot.application_form([Field("1", "file", "Attach")])
    assert not form_bot.application_form([Field("1", "text", "Search for jobs by keyword"),
                                           Field("2", "text", "country")])


@browser
def test_the_job_page_apply_link_is_opened_and_its_form_filled(tmp_path, monkeypatch):
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    result = form_bot.dry_run(
        settings(), "https://jobs.example.com/job/1", VALUES, SAVED, folder=tmp_path,
        prepare=serve({"https://jobs.example.com/job/1": JOB_PAGE,
                       "https://jobs.example.com/apply": FORM}))
    assert not result.stopped, result.stopped
    assert result.opened == "https://jobs.example.com/apply"
    labels = [a.field.label for a in result.answers]
    assert "First name *" in labels
    assert not any("Search" in label or label in ("city", "country") for label in labels)
    assert "Opened its Apply link: https://jobs.example.com/apply" in form_bot.report(result)


@browser
def test_an_email_first_sign_in_page_is_a_login(tmp_path, monkeypatch):
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    page = JOB_PAGE.replace("https://jobs.example.com/apply", "https://jobs.example.com/login")
    result = form_bot.dry_run(
        settings(), "https://jobs.example.com/job/1", VALUES, SAVED, folder=tmp_path,
        prepare=serve({"https://jobs.example.com/job/1": page,
                       "https://jobs.example.com/login": LOGIN}))
    assert result.stopped.startswith("the page asks for a login.")
    assert result.answers == []


@browser
def test_a_page_with_no_application_field_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    result = form_bot.dry_run(settings(), "https://jobs.example.com/jobs", VALUES, SAVED,
                              folder=tmp_path,
                              prepare=serve({"https://jobs.example.com/jobs": SEARCH_ONLY}))
    assert result.stopped.startswith("this page has no application form: its fields "
                                     "(\"Country\")")  # the keyword box is the search
    assert "/dryrun <job> <form link>" in result.stopped
    assert result.answers == []
