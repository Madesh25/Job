"""16 Nov /fetch: a glassdoor.co.in link was opened; "glassdoor.*" blocks Glassdoor in
every country."""

from jobengine import http
from jobengine.settings import load_settings
from jobengine.sweep import fulltext
from jobengine.track.expired_check import openable


def test_a_star_entry_is_the_site_in_every_country():
    blocked = ("pracuj.pl", "glassdoor.*")
    for host in ("glassdoor.co.in", "www.glassdoor.co.in", "glassdoor.de", "www.glassdoor.com"):
        assert http.host_in(host, blocked), host
    for host in ("notglassdoor.com", "glassdoorjobs.com", "example.com", "pracuj.pl.example.com"):
        assert not http.host_in(host, blocked), host
    assert http.host_in("www.pracuj.pl", blocked)


def test_the_config_blocks_glassdoor_in_india_everywhere_it_is_checked():
    s = load_settings("local", {})
    blocked = fulltext.blocked_hosts(s)
    url = "https://www.glassdoor.co.in/job-listing/devops-engineer-acme-JV_IC1.htm"
    assert http.host_in("www.glassdoor.co.in", blocked)
    assert not openable(url, blocked)
    assert openable("https://jobs.lever.co/acme/1", blocked)
