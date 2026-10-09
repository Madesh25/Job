"""Phase 7 (9 Oct): IND names that are the same company match; the IND guard and its nearest
names; /jd shows every board; the why line never names a gap."""

from datetime import date

import pytest
from test_phase5_fixes import companies
from test_review_one_at_a_time import make_desk

from jobengine.apply import pack
from jobengine.config_store import ConfigStore
from jobengine.ind_register import IndRegister, ind_name
from jobengine.screen import waiting
from jobengine.settings import load_settings
from jobengine.strategy import ind_refresh

S = load_settings("local", {})
REGISTER = IndRegister.from_names([
    "Infosys Limited", "TomTom International B.V.", "ING Bank N.V.",
    "Picnic Technologies B.V.", "Coöperatieve Rabobank U.A.", "HCL Technologies B.V.",
    "Tata Consultancy Services Netherlands B.V.", "Tech Mahindra (Nederland) B.V.",
    "Cognizant Technology Solutions Benelux B.V.", "Uber B.V.", "Delta Cloud B.V.",
    "Ingenico Group", "Wipro Technologies B.V.",
])


@pytest.mark.parametrize("company, legal", [
    ("Infosys (NL)", "Infosys Limited"),
    ("TomTom", "TomTom International B.V."),
    ("ING", "ING Bank N.V."),
    ("Picnic", "Picnic Technologies B.V."),
    ("Rabobank", "Coöperatieve Rabobank U.A."),
    ("HCLTech (NL)", "HCL Technologies B.V."),
    ("TCS (NL)", "Tata Consultancy Services Netherlands B.V."),
    ("Tech Mahindra (NL)", "Tech Mahindra (Nederland) B.V."),
    ("Uber (NL)", "Uber B.V."),
    ("Wipro (NL)", "Wipro Technologies B.V."),
])
def test_target_company_names_match_their_legal_name(company, legal):
    assert REGISTER.match(company) == legal


@pytest.mark.parametrize("company", ["Delta", "Ingen", "Mastercard", "Cognizant Cloud"])
def test_other_names_still_do_not_match(company):
    assert REGISTER.match(company) is None


def test_your_own_aliases_from_config():
    register = IndRegister.from_names(["Takeaway.com Group B.V.", "Miro Software B.V."])
    assert register.match("Miro (NL)") is None
    register.add_aliases("Miro (NL) = Miro Software\nnot a line")
    assert register.match("Miro (NL)") == "Miro Software B.V."
    assert ind_name("Coöperatieve Rabobank U.A.") == "rabobank"


def test_nearest_names_for_a_miss():
    assert REGISTER.closest("Cognizant (NL)")[0] == "Cognizant Technology Solutions Benelux B.V."


def test_the_guard_stops_a_third_dropping_and_shows_nearest_names():
    deps = ind_refresh.fake_deps(load_settings("prod", {}), writer=lambda *a: pytest.fail())
    deps.companies = lambda: companies(28)[:28]
    report = ind_refresh.refresh(deps)
    assert report.changes == []
    text = report.text()
    assert "Not found on the register, nearest names" in text
    assert "- Gone 0 B.V.: " in text


def test_warning_says_not_written_outside_prod():
    report = ind_refresh.IndReport(checked=1, written=False, changes=[
        ind_refresh.IndChange("ING", "Verified", "Not listed")])
    assert "will rank lower once this is written (prod)" in report.text()
    report.written = True
    assert "Its jobs now rank lower" in report.text()


def test_jd_list_gives_every_board_a_share():
    d = make_desk()
    for i in range(30):  # more LinkedIn jobs than the whole list holds
        d.repo.rows[f"li-{i}"] = {**d.repo.rows["li-no-jd"], "Company": f"Co\n{i}"}
    waiting.note(d.state, ["pl-clean"], date(2026, 10, 9))
    d.repo.rows["pl-clean"].update({"Screen verdict": "Unscreened", "Board": "Pracuj.pl"})
    text = d.waiting_list().text
    assert "\nPracuj.pl:\n- " in text and "more LinkedIn jobs" in text
    assert "Co\n" not in text  # one line per company


def test_why_line_skips_a_detail_with_a_gap():
    config = ConfigStore.fake()
    details = ["Operating EC2, S3, Dynamo, Lambda, Bedrock services", "Kubernetes on AWS"]
    line = pack.why_line(S, config, details, ["Dynamo", "Lambda", "Bedrock"])
    assert "Kubernetes on AWS" in line and "Dynamo" not in line
    assert pack.why_line(S, config, details[:1], ["Lambda"]) is None
