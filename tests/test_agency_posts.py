"""Agency posts in every country are kept and marked, and get no cold mails (30 Sep)."""

from test_fetch_contacts import two_applied
from test_visa import REF, REGISTER

from jobengine.config_store import ConfigStore
from jobengine.outreach.budget import JobFacts, decide
from jobengine.outreach.planner import facts
from jobengine.screen import visa
from jobengine.screen.models import Extraction, JobRow, Quoted


def pl(company):
    return JobRow(page_id="p", company=company, role="DevOps", country="Poland")


def check(row, ext=None, config=None):
    return visa.visa_checks(row, ext or Extraction(), REF,
                            config or ConfigStore.from_values({}), REGISTER).flags


def test_agencies_are_flagged_in_every_country():
    assert check(pl("Hays Poland")) == [visa.AGENCY]
    assert check(pl("Antal")) == [visa.AGENCY]
    assert check(pl("Verita HR Polska Sp. z o.o.")) == [visa.AGENCY]
    assert check(pl("GeekSoft Staffing")) == [visa.AGENCY]
    assert check(pl("Vistula Cloud")) == []
    said = Extraction(agency_posting=Quoted(value=True, quote="for our client"))
    assert check(pl("Some Company"), said) == [visa.AGENCY]
    config = ConfigStore.from_values({"agency_names": "Scalo, apreel"})
    assert check(pl("Scalo Sp. z o.o."), config=config) == [visa.AGENCY]


def test_no_outreach_for_an_agency_post():
    job = JobFacts(verdict="Apply high", sponsorship="Stated yes", agency=True)
    decision = decide(job, allowance=10, used=0, capacity=10)
    assert not decision.outreach and "recruitment agency" in decision.reason
    assert facts({"Screen verdict": "Apply high", "Visa flags": [visa.AGENCY]}, REF).agency
    assert facts({"Visa flags": [visa.AGENCY_IE]}, REF).agency
    assert not facts({"Visa flags": [visa.ON_IND]}, REF).agency


def test_fetchcontacts_skips_agency_posts():
    d = two_applied()
    d.repo.rows["pl-clean"]["Visa flags"] = [visa.AGENCY]
    listing = d.fetch_contacts_command()[0].text
    assert "- Vistula Cloud, DevOps Engineer: agency post, no cold mails" in listing
    replies = d.tap("fx:2026-10-01")
    texts = [r.text for r in replies]
    assert texts[1].startswith("Vistula Cloud, DevOps Engineer: a recruitment agency's post")
    assert "Agency posts, no cold mails: Vistula Cloud, DevOps Engineer" in texts[-1]
    assert not d.repo.rows["pl-clean"].get("Contacts")


def test_fetch_marks_agency_posts_when_it_saves_them():
    # 1 Oct test T10: Verks Recruitment and VERITA HR were saved unmarked, and screening
    # skipped them (Tech mismatch) before the agency check ran. /fetch now marks them.
    from test_runner import TODAY, S

    from jobengine.bot_state import FakeBotState
    from jobengine.config_store import ConfigRow, ConfigStore
    from jobengine.sweep.runner import fake_deps, run_sweep

    deps = fake_deps(S)
    store = ConfigStore.fake()
    rows = dict(store._rows)
    rows["agency_names"] = ConfigRow(key="agency_names", value="Liffey Analytics, Odra Systems")
    deps.config = lambda: ConfigStore(rows)
    run_sweep(S, deps, TODAY, state=FakeBotState())
    flags = {v["Company"]: v.get("Visa flags") for v in deps.repo.rows.values()}
    assert flags["Liffey Analytics Ltd"] == ["Agency posting (IE)"]
    assert flags["Odra Systems S.A."] == ["Agency posting"]
    assert flags["Northwind Cloud"] is None


def test_agency_words_in_the_company_name():
    from jobengine.config_store import ConfigStore
    from jobengine.screen.visa import agency_by_name

    config = ConfigStore.fake()
    assert agency_by_name("Verks Recruitment", config)
    assert agency_by_name("VERITA HR POLSKA", config)
    assert not agency_by_name("Mastercard", config)


def test_fetch_marks_an_agency_row_saved_before_and_keeps_its_flags():
    # 1 Oct Phase 2 R4: Verks Recruitment was saved before /fetch marked agencies and was
    # only updated, so it stayed unmarked.
    from test_runner import TODAY, S

    from jobengine.bot_state import FakeBotState
    from jobengine.config_store import ConfigRow, ConfigStore
    from jobengine.sweep.runner import fake_deps, run_sweep

    deps = fake_deps(S)
    run_sweep(S, deps, TODAY, state=FakeBotState())  # saves the jobs unmarked
    page = next(pid for pid, v in deps.repo.rows.items()
                if v["Company"] == "Liffey Analytics Ltd")
    deps.repo.rows[page]["Visa flags"] = ["Not on IND register"]
    rows = dict(ConfigStore.fake()._rows)
    rows["agency_names"] = ConfigRow(key="agency_names", value="Liffey Analytics")
    deps.config = lambda: ConfigStore(rows)
    run_sweep(S, deps, TODAY, state=FakeBotState())  # the same jobs again: updates
    assert deps.repo.rows[page]["Visa flags"] == ["Not on IND register", "Agency posting (IE)"]
