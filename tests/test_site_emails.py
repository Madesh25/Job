"""Free contacts from the company's own website (27 Oct, your request). Made-up pages."""

from datetime import date

from test_contacts_ai_search import PL, deps, no_waterfall  # noqa: F401  (a fixture)

from jobengine.bot_state import FakeBotState
from jobengine.contacts import site_emails
from jobengine.contacts.finder import find_contacts

TODAY = date(2026, 10, 27)
HOME = """<html><body><nav>
<a href="/careers">Careers</a> <a href="https://vistula.example.com/kontakt">Kontakt</a>
<a href="https://other.example.org/jobs">Partner jobs</a> <a href="/pricing">Pricing</a>
<a href="mailto:info@vistula.example.com">info@vistula.example.com</a>
</nav><p>Sales: sales@vistula.example.com</p></body></html>"""
CAREERS = """<html><body><h1>Join us</h1>
<p>Questions about a role? Write to careers@vistula.example.com</p>
<p>Your recruiter: Anna Kowalska, anna.kowalska@vistula.example.com</p>
<p>Agency partner: jan@agency.example.net</p></body></html>"""
CONTACT = """<html><body><p>Office: biuro@vistula.example.com</p>
<p>Talent Acquisition Lead Ola Nowak ola.nowak@vistula.example.com</p>
<p>CEO Piotr Wolny piotr.wolny@vistula.example.com</p>
<script>var x = "hidden@vistula.example.com";</script></body></html>"""
PAGES = {"https://vistula.example.com/": HOME,
         "https://vistula.example.com/careers": CAREERS,
         "https://vistula.example.com/kontakt": CONTACT}


def reader(pages=PAGES, log=None):
    def read(url):
        if log is not None:
            log.append(url)
        if url not in pages:
            raise OSError("HTTP 404")
        return pages[url]
    return read


def test_only_its_own_careers_and_contact_pages_careers_first():
    assert site_emails.links(HOME, "https://vistula.example.com/", "vistula.example.com") == [
        "https://vistula.example.com/careers", "https://vistula.example.com/kontakt"]


def test_hiring_mailboxes_and_named_recruiters_only_never_guessed():
    found, pages = site_emails.search("vistula.example.com", reader(), ())
    assert pages == list(PAGES)
    got = {f.email: f for f in found}
    assert set(got) == {"careers@vistula.example.com", "anna.kowalska@vistula.example.com",
                        "ola.nowak@vistula.example.com"}
    assert got["careers@vistula.example.com"].generic
    assert got["anna.kowalska@vistula.example.com"].name == "Anna Kowalska"
    assert not got["ola.nowak@vistula.example.com"].generic  # "Talent Acquisition" line
    # Left out: info@, sales@, biuro@ (not hiring), the CEO on the contact page, another
    # domain, an address only inside a script.


def test_a_site_that_cannot_be_read_gives_nothing():
    found, pages = site_emails.search("nothing.example.com", reader(), ())
    assert (found, pages) == ([], [])


def test_the_result_is_kept_for_30_days():
    state = FakeBotState()
    found = [site_emails.SiteEmail("careers@x.example.com", "careers@x.example.com", True,
                                   "https://x.example.com/careers")]
    site_emails.remember(state, "x", TODAY, found)
    assert site_emails.cached(state, "x", date(2026, 11, 20)) == found
    assert site_emails.cached(state, "x", date(2026, 11, 27)) is None
    assert site_emails.cached(state, "y", TODAY) is None


def test_the_finder_adds_them_before_any_paid_lookup(no_waterfall):  # noqa: F811
    log = []
    d = deps()
    d.read_page = reader(log=log)
    result = find_contacts(d, PL)
    names = {c.email: c for c in result.contacts}
    anna = names.get("anna.kowalska@vistula.example.com")
    assert anna is not None and anna.source == "Careers page" and anna.type == "Recruiter/TA"
    assert anna.notes == "on the company website: https://vistula.example.com/careers"
    assert "company website: 3 address(es) found on vistula.example.com" in result.notes
    assert not any("linkedin" in url for url in log)
    log.clear()
    find_contacts(d, PL)  # the same company again: from Bot State, no page read
    assert log == []
