"""Autopilot A2 (26 Oct): the company of a job you applied to goes into Target Companies,
with Undo; only with Notion Config apply.add_companies = true. Made-up companies."""

from test_telegram_bot import Clock, settings

from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track.runner import DailyReport


def desk(on=True, company="Nordlicht Systems", url="https://boards.greenhouse.io/nordlicht/jobs/9"):
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    base = ConfigStore.fake().all()
    d.deps.config = lambda: ConfigStore.from_values(
        {**base, "apply.add_companies": "true" if on else "false"})
    d.repo.rows["pl-clean"].update({"Status": "Resume built", "Company": company, "URL": url,
                                    "Country": "Poland"})
    return d


def test_i_applied_adds_the_company_with_its_board_and_undo():
    d = desk()
    replies = d.tap("ia:pl-clean")
    assert replies[0].text.startswith("Marked as applied on ")
    added = replies[1]
    assert added.text.startswith("Added Nordlicht Systems to Target Companies with its "
                                 "Greenhouse job board: https://boards.greenhouse.io/nordlicht")
    assert added.buttons[0][0] == "Undo"
    [row] = d.companies.rows.values()
    assert row["Region"] == "Poland" and row["Notes"].startswith("Added from your application")


def test_off_by_default_and_never_twice_or_for_agencies():
    d = desk(on=False)
    assert not any("Target Companies" in r.text for r in d.tap("ia:pl-clean"))
    assert d.companies.rows == {}
    d = desk()
    known = d.reference().companies[0].name
    assert d.add_applied_companies([]) == []
    d.repo.rows["pl-clean"]["Company"] = known
    assert d.add_applied_companies(["pl-clean"]) == []
    d.repo.rows["pl-clean"]["Company"] = "Hays Poland"
    assert d.add_applied_companies(["pl-clean"]) == []
    assert d.companies.rows == {}


def test_a_linkedin_link_never_becomes_the_careers_url():
    d = desk(url="https://www.linkedin.com/jobs/view/123")
    [added] = d.add_applied_companies(["pl-clean"])
    assert "no link yet" in added.text
    assert "Careers URL" not in next(iter(d.companies.rows.values()))


def test_a_confirmation_mail_adds_it_too():
    d = desk()
    report = DailyReport(day=FAKE_TODAY, since=Clock()(), applied_jobs=["pl-clean"])
    texts = [r.text for r in d.report_replies(report)]
    assert any(t.startswith("Added Nordlicht Systems to Target Companies") for t in texts)
