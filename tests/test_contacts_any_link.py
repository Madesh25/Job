"""Phase 12 requests (28 Oct): /contacts works for a job link not in Notion yet (the job is
saved first), and for a LinkedIn job with the company and country typed after the link (the
bot never opens LinkedIn). The same link with or without utm parts finds the same job."""

from test_add_link import PAGE, URL, desk_with
from test_telegram_bot import settings

from jobengine.screen import add_link
from jobengine.screen.runner import find_row
from jobengine.sweep.normalize import Rules

RULES = Rules.from_config(settings().sweep)
LINKEDIN = "https://www.linkedin.com/jobs/view/4099999999"


def found(d, company):
    return [(pid, r) for pid, r in d.repo.rows.items() if r.get("Company") == company]


def test_company_and_country_typed_after_a_link():
    parse = add_link.parse_company_country
    assert parse("Acme Cloud, Poland, DevOps Engineer", RULES) == (
        "Acme Cloud", "Poland", "DevOps Engineer")
    assert parse("Acme Cloud, Warsaw", RULES) == ("Acme Cloud", "Poland", "the role")
    assert parse("Acme, NL, Platform Engineer, Kubernetes", RULES) == (
        "Acme", "Netherlands", "Platform Engineer, Kubernetes")
    assert parse("Acme Cloud", RULES) is None
    assert parse("Acme Cloud, Germany", RULES) is None  # not one of your countries
    assert parse("", RULES) is None


def test_a_company_job_link_not_in_notion_is_saved_then_its_contacts_found():
    d = desk_with(PAGE)
    texts = [r.text for r in d.contacts_command(URL)]
    assert texts[0].startswith("Saved Wisla Data, DevOps Engineer (Kraków, Poland) as a new "
                               "job (not screened: /add <link> screens it")
    [(pid, row)] = found(d, "Wisla Data")
    assert (row["Status"], row["Board"], row["Country"]) == ("New", "Company site", "Poland")
    assert any("Description source: /contacts (full)" in b for b in d.repo.read_body(pid))
    assert any("Wisla Data" in t for t in texts[1:])  # the contacts answer follows
    again = [r.text for r in d.contacts_command(URL)]
    assert not again[0].startswith("Saved") and len(found(d, "Wisla Data")) == 1


def test_a_linkedin_link_asks_for_the_company_and_country():
    d = desk_with(PAGE)
    opened = []
    d.get_slow_page = lambda url: opened.append(url)
    [reply] = d.contacts_command(LINKEDIN)
    assert reply.text.startswith("The bot never opens www.linkedin.com pages, so it needs the "
                                 "company and the country from you.")
    assert f"/contacts {LINKEDIN} Acme Cloud, Poland, DevOps Engineer" in reply.text
    assert opened == [] and found(d, "Acme Cloud") == []


def test_a_linkedin_job_with_company_and_country_is_saved_and_searched():
    d = desk_with(PAGE)
    opened = []
    d.get_slow_page = lambda url: opened.append(url)
    texts = [r.text for r in d.contacts_command(f"{LINKEDIN} Wisla Data, Krakow, SRE")]
    assert texts[0].startswith("Saved Wisla Data, SRE (Poland) as a new job (not screened; "
                               "the bot never opens that site).")
    [(_pid, row)] = found(d, "Wisla Data")
    assert (row["Board"], row["Country"], row["URL"]) == ("LinkedIn", "Poland", LINKEDIN)
    assert opened == []  # LinkedIn is never opened


def test_the_same_link_with_or_without_utm_finds_the_same_job():
    d = desk_with(PAGE)
    pid = next(iter(d.repo.rows))
    d.repo.rows[pid]["URL"] = "https://www.adzuna.pl/details/123?utm_medium=api&utm_source=x"
    assert find_row(d.repo, "https://www.adzuna.pl/details/123").page_id == pid
    d.repo.rows[pid]["URL"] = "https://www.adzuna.pl/details/456"
    assert find_row(d.repo, "https://www.adzuna.pl/details/456?utm_source=y").page_id == pid
    assert find_row(d.repo, "https://www.adzuna.pl/details/789") is None
