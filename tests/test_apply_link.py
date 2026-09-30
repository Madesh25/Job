"""An Adzuna link is swapped for the employer's page when you approve a resume."""

from test_review_one_at_a_time import approve_resume, make_desk

from jobengine import http
from jobengine.apply import link

ADZ = "https://www.adzuna.pl/details/5901137322?utm_medium=api"
EMPLOYER = "https://careers.vistula.example.com/jobs/77"


def test_land_link():
    assert link.land_link(ADZ) == "https://www.adzuna.pl/land/ad/5901137322"
    land = "https://www.adzuna.nl/land/ad/42?se=x"
    assert link.land_link(land) == land
    assert link.land_link("https://jobs.example.com/details/1") is None


def test_resolve():
    assert link.resolve(ADZ, lambda url: (EMPLOYER, "")) == link.Link(
        url=EMPLOYER, employer=True, adzuna=ADZ)
    # The redirect stays on Adzuna (the region page), or Adzuna refuses the request.
    assert link.resolve(ADZ, lambda url: (ADZ, "")) == link.Link(url=ADZ, adzuna=ADZ)

    def refused(url):
        raise http.HttpError("GET failed: HTTP 403", 403)

    assert link.resolve(ADZ, refused) == link.Link(url=ADZ, adzuna=ADZ)
    other = "https://jobs.example.com/pl-clean"
    assert link.resolve(other, refused) == link.Link(url=other)


def adzuna_desk():
    d = make_desk()
    d.repo.rows["pl-clean"]["URL"] = ADZ
    return d


def test_apply_here_gives_the_employer_page():
    d = adzuna_desk()
    opened = []
    d.get_page = lambda url: (opened.append(url), (EMPLOYER, ""))[1]
    last = approve_resume(d)[-1].text
    assert opened == ["https://www.adzuna.pl/land/ad/5901137322"]
    assert last.startswith(f"Resume approved and saved. Apply here: {EMPLOYER}\n"
                           "(The employer's own page. The Adzuna link is kept on the Notion "
                           "page.)")
    assert d.repo.rows["pl-clean"]["URL"] == EMPLOYER
    assert f"Adzuna link: {ADZ}" in d.repo.read_body("pl-clean")


def test_blocked_adzuna_link_gets_a_search_and_the_vpn_note():
    d = adzuna_desk()  # the fake desk has no network: the redirect cannot be followed
    last = approve_resume(d)[-1].text
    assert f"Apply here: {ADZ}\n" in last
    assert ('Adzuna may say "not available in your region" outside Poland. Find the job on '
            "Vistula Cloud's own site: https://www.google.com/search?q=Vistula+Cloud+DevOps+"
            "Engineer+careers , or open the Adzuna link with a VPN set to Poland.") in last
    assert d.repo.rows["pl-clean"]["URL"] == ADZ


def test_other_links_are_not_opened():
    d = make_desk()
    d.get_page = lambda url: (_ for _ in ()).throw(AssertionError("no page read"))
    assert "Apply here: https://jobs.example.com/pl-clean\n" in approve_resume(d)[-1].text
