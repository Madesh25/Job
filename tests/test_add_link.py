"""/add <job link> (23 Oct, your request): a company's job page is read, the job saved and
screened, the company added to Target Companies, and the resume built. Made-up company."""

import json

from test_telegram_bot import Clock, settings

from jobengine import http
from jobengine.screen import add_link
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY

POSTING = {
    "@context": "https://schema.org", "@type": "JobPosting", "title": "DevOps Engineer",
    "hiringOrganization": {"@type": "Organization", "name": "Wisla Data"},
    "jobLocation": [{"@type": "Place", "address": {"addressLocality": "Krakow",
                                                     "addressCountry": "PL"}}],
    "datePosted": "2026-10-20", "validThrough": "2026-12-01",
    "description": "<p>" + "Run Kubernetes, Terraform and AWS for our data platform. " * 8
                   + "</p><p>3 years of experience with CI/CD in GitLab.</p>",
}
PAGE = ('<html><head><title>DevOps Engineer | Wisla Data</title><script '
        f'type="application/ld+json">{json.dumps(POSTING)}</script></head>'
        '<body><main>Apply now</main></body></html>')
URL = "https://apply.workable.com/wisla-data/j/AB12CD/"


def desk_with(page=PAGE):
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.get_page = lambda url: (url, page)
    return d


def test_the_job_page_is_read():
    job = add_link.read_page(URL, PAGE)
    assert (job.title, job.company, job.location) == ("DevOps Engineer", "Wisla Data",
                                                      "Krakow, Poland")
    assert job.description.startswith("Run Kubernetes")
    assert add_link.careers_url(URL) == ("https://apply.workable.com/wisla-data/", "Custom")
    assert add_link.careers_url("https://boards.greenhouse.io/acme/jobs/1") == (
        "https://boards.greenhouse.io/acme", "Greenhouse")
    assert add_link.careers_url("https://careers.example.com/jobs/1") == (
        "https://careers.example.com/", "Unknown")
    assert add_link.known_company("Citi", ["Citi (IE)", "Acme"]) == "Citi (IE)"


def test_add_saves_screens_adds_the_company_and_builds_the_resume():
    d = desk_with()
    texts = [r.text for r in d.add_command(URL)]
    assert texts[0] == "Saved: Wisla Data, DevOps Engineer (Kraków, Poland). Screening it now..."
    row = next(r for r in d.repo.rows.values() if r.get("Company") == "Wisla Data")
    assert row["Board"] == "Company site" and row["Country"] == "Poland"
    assert row["Status"] == "Resume built"  # you apply to it: Approved, then the resume
    [company] = d.companies.rows.values()
    assert company["Careers URL"] == "https://apply.workable.com/wisla-data/"
    assert (company["Region"], company["Active"], company["ATS platform"]) == (
        "Poland", True, "Custom")
    added = next(t for t in texts if t.startswith("Added Wisla Data to Target Companies"))
    assert "Every /fetch checks it from now on." in added
    assert any("resume" in t.lower() for t in texts[texts.index(added) + 1:])
    # Undo unticks Active.
    page_id = next(iter(d.companies.rows))
    assert d.tap(f"tu:{page_id}")[0].text.startswith("Undone")
    assert d.companies.rows[page_id]["Active"] is False
    # The same link again: the saved row is used, its description is not added twice.
    job_id = next(k for k, r in d.repo.rows.items() if r.get("Company") == "Wisla Data")
    assert d.add_command(URL)[0].text.startswith("Found: Wisla Data, DevOps Engineer")
    assert d.repo.read_body(job_id).count("Description source: /add (full)") == 1


def test_a_known_company_or_an_agency_is_not_added_again():
    d = desk_with()
    known = d.reference().companies[0].name
    assert d._add_company(known, URL, "Poland")[0].text == (
        f"{known} is already in Target Companies.")
    assert "agency" in d._add_company("Hays Recruitment", URL, "Poland")[0].text
    assert d.companies.rows == {}


def test_linkedin_and_blocked_sites_are_never_opened():
    d = desk_with()
    opened = []
    d.get_page = lambda url: opened.append(url)
    for link in ("https://www.linkedin.com/jobs/view/4099", "https://www.pracuj.pl/praca/x"):
        text = d.add_command(link)[0].text
        assert text.startswith("The bot never opens") and "/jd" in text
    assert opened == []
    assert d.add_command("")[0].text.startswith("Send /add <job link>")


def test_a_page_without_a_description_asks_for_the_text():
    d = desk_with("<html><title>Jobs</title><body>Loading...</body></html>")
    text = d.add_command("https://careers.example.com/jobs/1")[0].text
    assert "description" in text and "/jd https://careers.example.com/jobs/1" in text

    def refuse(url):
        raise http.HttpError("GET failed: HTTP 403", 403)

    d.get_page = refuse
    assert d.add_command("https://careers.example.com/jobs/1")[0].text.startswith(
        "Could not open the job page")
