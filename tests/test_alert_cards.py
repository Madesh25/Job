"""Alert cards where one link holds the whole card, and IrishJobs links that are not jobs
(1 Oct tests T3, T4, T8). Shapes copied from real alerts, with made-up links and no
personal data."""

from jobengine.gmail_reader import GmailMessage
from jobengine.settings import load_settings
from jobengine.sweep.normalize import Rules, place
from jobengine.sweep.sources import gmail_alerts

S = load_settings("local", {})
CFG = S.sweep["gmail"]
RULES = Rules.from_config(S.sweep)


def parse(sender, html):
    return gmail_alerts.parse_alert(GmailMessage(id="m", sender=sender, subject="s", html=html),
                                    CFG)


def test_linkedin_card_inside_one_link_gives_company_and_place():
    html = ('<a href="https://www.linkedin.com/comm/jobs/view/4300000001/?t=1"><table>'
            '<tr><td>Cloud Infrastructure Engineer</td></tr><tr><td>eir Ireland · Dublin'
            '</td></tr><tr><td>Actively recruiting</td></tr></table></a>'
            '<a href="https://www.linkedin.com/comm/jobs/view/4300000002/?t=1"><p>DevOps Engineer'
            '</p><p>MDLN SIA · Zeist (On-site)</p><p>Easy Apply</p></a>')
    first, second = parse("LinkedIn <jobalerts-noreply@linkedin.com>", html)
    assert (first.title, first.company, first.location_text) == (
        "Cloud Infrastructure Engineer", "eir Ireland", "Dublin")
    assert first.posting_id == "4300000001"
    assert place(first.location_text, RULES, first.location_area) == ("Ireland", "Dublin")
    assert (second.title, second.company) == ("DevOps Engineer", "MDLN SIA")
    assert place(second.location_text, RULES, second.location_area) == ("Netherlands", "Zeist")
    assert "Easy Apply" not in second.location_area


def test_justjoin_card_starts_with_company_and_city():
    html = ('<a href="https://justjoin.it/job-offer/acme-site-reliability-engineer-warszawa-devops'
            '?utm_source=mail"><div>Acme Software</div><div>Warszawa</div>'
            '<div>Site Reliability Engineer</div><div>Undisclosed salary</div><div>Remote</div>'
            '<div>B2B</div><div>Be the first to apply!</div><div>Apply</div></a>')
    [job] = parse("justjoin.it <no-reply@justjoin.it>", html)
    assert (job.title, job.company, job.location_text) == (
        "Site Reliability Engineer", "Acme Software", "Warszawa")
    assert job.posting_id == "acme-site-reliability-engineer-warszawa-devops"
    assert place(job.location_text, RULES, job.location_area)[0] == "Poland"
    assert "Apply" not in job.location_area


def test_irishjobs_more_and_footer_links_are_not_jobs():
    card = ('<table><tr><td><a href="https://click.example.ie/f/a/1">Cloud Engineer</a></td></tr>'
            '<tr><td>Cpl Resources</td></tr><tr><td>Dublin, County Dublin</td></tr>'
            '<tr><td>Permanent Contract</td></tr></table>'
            '<p>Cloud Engineer for the Data Act team...</p>'
            '<a href="https://click.example.ie/f/a/2">more</a>')
    footer = ''.join(f'<a href="https://click.example.ie/f/a/{i}">{text}</a>' for i, text in
                     enumerate(["See all matching jobs",
                                "Change criteria for jobs by email", "Contact us",
                                "Terms of use", "Privacy policy"], start=3))
    [job] = parse("IrishJobs <info@jobs.irishjobs.ie>", card + footer)
    assert (job.title, job.company, job.location_text) == (
        "Cloud Engineer", "Cpl Resources", "Dublin, County Dublin")


def test_more_cities_are_known():
    for text, expected in [("Alkmaar", ("Netherlands", "Alkmaar")),
                           ("Leeuwarden (Hybrid)", ("Netherlands", "Leeuwarden")),
                           ("Cracow (Hybrid)", ("Poland", "Kraków")),
                           ("County Wicklow, Ireland (Hybrid)", ("Ireland", "Wicklow"))]:
        assert place(text, RULES) == expected
