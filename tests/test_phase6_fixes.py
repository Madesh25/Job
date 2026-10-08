"""Phase 6 (8 Oct): blocked sites wait for /jd and are not opened on every /fetch; the job
link is checked before a resume is built; the IND name column; domain suggestions."""

from datetime import date

import httpx
import pytest
from test_backfill_and_fixes import PAGE, row
from test_review_one_at_a_time import make_desk

from jobengine import http
from jobengine.apply import link
from jobengine.bot_state import FakeBotState
from jobengine.contacts.finder import base_domain, domain_options
from jobengine.ind_register import IndRegister, parse_register_html
from jobengine.notion_repo import FakeJobsRepo
from jobengine.screen import waiting
from jobengine.screen.models import ScreenSummary
from jobengine.screen.runner import waiting_for_jd
from jobengine.settings import load_settings
from jobengine.sweep import backfill, fulltext

TODAY = date(2026, 10, 8)
S = load_settings("local", {})


# ---------------------------------------------------------------- blocked sites and /jd


def test_blocked_sites_come_from_the_config():
    assert {"pracuj.pl", "irishjobs.ie", "jobs.ie"} <= set(fulltext.blocked_hosts(S))
    assert http.host_in("www.pracuj.pl", ("pracuj.pl",))
    assert not http.host_in("notpracuj.pl", ("pracuj.pl",))


def test_a_blocked_row_is_noted_without_being_opened():
    repo = FakeJobsRepo([row("p1", "Pracuj.pl", "https://www.pracuj.pl/praca/x,oferta,1"),
                         row("a1", "Company site", "https://jobs.example.com/a1")])
    state, read = FakeBotState(), []
    filled = backfill.fill_waiting(repo, lambda url: read.append(url) or (url, PAGE), set(),
                                   state=state, today=TODAY, blocked=("pracuj.pl",))
    assert filled == ["Co a1, DevOps Engineer"]
    assert read == ["https://jobs.example.com/a1"]  # Pracuj.pl never opened
    assert set(waiting.noted(state)) == {"p1"}
    # Listed in /jd next to LinkedIn rows.
    assert [r.page_id for r in waiting_for_jd(repo, state)] == ["p1"]


def test_a_failed_row_is_not_read_again_for_a_week():
    repo = FakeJobsRepo([row("t1", "JustJoin IT", "https://click.example.com/track/1")])
    state, read = FakeBotState(), []

    def tiny(url):
        read.append(url)
        return url, "<html>tiny</html>"

    for _ in range(3):  # three /fetch runs the same day
        backfill.fill_waiting(repo, tiny, set(), state=state, today=TODAY)
    assert read == ["https://click.example.com/track/1"]
    later = date(2026, 10, 16)
    backfill.fill_waiting(repo, lambda url: (url, PAGE), set(), state=state, today=later)
    assert repo.rows["t1"]["body"] and waiting.noted(state) == {}  # filled: no longer waits


def test_site_guard_asks_a_refusing_site_once_per_sweep():
    calls = []

    def page(url):
        calls.append(url)
        raise http.HttpError("GET failed: HTTP 403", 403, "www.irishjobs.example")

    guard = fulltext.SiteGuard(page, ("pracuj.pl",))
    for url in ("https://www.irishjobs.example/a", "https://www.irishjobs.example/b",
                "https://www.pracuj.pl/x"):
        with pytest.raises(http.HttpError):
            guard(url)
    assert calls == ["https://www.irishjobs.example/a"]


def test_get_page_stops_a_tracking_link_before_a_blocked_site():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://www.pracuj.pl/praca/1"})

    http.set_transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(http.HttpError) as exc:
            http.get_page("https://click.alerts.example/1", S, blocked=("pracuj.pl",))
    finally:
        http.set_transport(None)
    assert seen == ["click.alerts.example"]
    assert exc.value.status == 403 and "blocks the bot" in str(exc.value)


def test_jd_lists_every_board():
    d = make_desk()
    waiting.note(d.state, ["pl-clean"], TODAY)
    d.repo.rows["pl-clean"]["Screen verdict"] = "Unscreened"
    d.repo.rows["pl-clean"]["Board"] = "Pracuj.pl"
    text = d.waiting_list().text
    assert "(1 LinkedIn, " in text and "1 Pracuj.pl" in text
    assert "\nPracuj.pl:\n- " in text


def test_collect_counts_the_real_waiting_rows():
    summary = ScreenSummary(waiting_for_jd=None)
    assert summary.text().endswith("skipped).")
    d = make_desk()
    text = d._summary_text(ScreenSummary(waiting_for_jd=None))
    assert "need the JD" in text and "0 waiting for JD" not in text


def test_screen_with_a_word_shows_the_forms():
    d = make_desk()
    assert d.screen_replies("pending")[0].text.startswith("/screen screens the jobs")


# ---------------------------------------------------------------- is the posting open?


def gone(url):
    raise http.HttpError("GET failed: HTTP 404", 404, "jobs.example.com")


def test_closed_reason():
    assert "HTTP 404" in link.closed_reason("https://jobs.example.com/1", gone, TODAY)
    assert link.closed_reason("https://www.linkedin.com/jobs/view/1", gone, TODAY) is None
    page = "<html><body><h1>Sorry, this job is no longer available</h1></body></html>"
    assert "no longer available" in link.closed_reason(
        "https://jobs.example.com/1", lambda url: (url, page), TODAY)
    # Words inside scripts (translations of a whole site) do not count.
    script = "<script>var t='This job is no longer available'</script><p>Apply now</p>"
    assert link.closed_reason("https://jobs.example.com/1", lambda u: (u, script), TODAY) is None
    ld = ('<script type="application/ld+json">{"@type": "JobPosting", '
          '"validThrough": "2026-09-30T00:00:00Z"}</script>')
    assert "closed on 2026-09-30" in link.closed_reason(
        "https://jobs.example.com/1", lambda u: (u, ld), TODAY)
    assert "job list" in link.closed_reason(
        "https://boards.greenhouse.io/x/jobs/1",
        lambda u: ("https://boards.greenhouse.io/x?error=true", ""), TODAY)

    def offline(url):
        raise http.HttpError("GET failed: ConnectTimeout")

    assert link.closed_reason("https://jobs.example.com/1", offline, TODAY) is None


def test_approve_warns_on_a_closed_posting():
    d = make_desk()
    d.get_page = gone
    [warning] = d.tap("ap:pl-clean")
    assert "the posting looks closed (the job page answers HTTP 404" in warning.text
    assert [data for _, data in warning.buttons] == ["ab:pl-clean", "xe:pl-clean"]
    assert d.repo.rows["pl-clean"]["Status"] == "Screened"  # nothing changed yet
    replies = d.tap("ab:pl-clean")
    assert replies[0].document is not None  # built anyway
    assert d.repo.rows["pl-clean"]["Status"] == "Resume built"


def test_skip_as_expired():
    d = make_desk()
    d.get_page = gone
    d.tap("ap:pl-clean")
    replies = d.tap("xe:pl-clean")
    assert replies[0].text.startswith("Marked expired: ")
    assert d.repo.rows["pl-clean"]["Status"] == "Expired"


# ---------------------------------------------------------------- IND register


def test_ind_names_in_row_headers_are_read():
    html = ("<table><thead><tr><th>Organisation</th><th>KvK number</th></tr></thead><tbody>"
            "<tr><th scope='row'>Tulip Data B.V.</th><td>12345678</td></tr>"
            "<tr><th scope='row'>Canal Payments N.V.</th><td>87654321</td></tr>"
            "</tbody></table>")
    names = parse_register_html(html)
    assert names == ["Tulip Data B.V.", "Canal Payments N.V."]
    assert IndRegister.from_names(names).match("Canal Payments") == "Canal Payments N.V."


def test_ind_a_number_column_is_never_the_names():
    html = ("<table><tr><th>Organisation</th><th>KvK</th></tr>"
            "<tr><td>12345678</td><td>Tulip Data B.V.</td></tr>"
            "<tr><td>87654321</td><td>Canal Payments N.V.</td></tr></table>")
    assert parse_register_html(html) == ["Tulip Data B.V.", "Canal Payments N.V."]


# ---------------------------------------------------------------- domain suggestions


def test_domain_options_come_from_the_job_pages_only():
    jd = ("Mail jobs@acme-cloud.nl or apply on https://acme.wd3.myworkdayjobs.com/x. "
          "We run https://kubernetes.io and https://www.terraform.io, see x.com/acme.")
    options = domain_options("Acme Cloud B.V.", ["https://careers.acmecloud.com/jobs/1",
                                                 "https://www.pracuj.pl/praca/1"], jd)
    assert options == ["acmecloud.com", "acme-cloud.nl"]
    assert domain_options("Nobody", [], "") == []
    assert base_domain("careers.example.com.pl") == "example.com.pl"
