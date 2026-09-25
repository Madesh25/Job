import pytest

from jobengine.config_store import ConfigStore
from jobengine.ind_register import IndRegister, ind_name
from jobengine.reference import Reference
from jobengine.screen import visa
from jobengine.screen.models import Extraction, JobRow, Quoted

REF = Reference.fake()
REGISTER = IndRegister.from_names(
    ["Maastricht Logistics B.V.", "Delta Cloud Holding N.V.", "Tulip Data Nederland B.V.",
     "Windmill Software Netherlands B.V."]
)
CONFIG = ConfigStore.fake()


def nl(company):
    return JobRow(page_id="p", company=company, role="DevOps", country="Netherlands")


def ie(company):
    return JobRow(page_id="p", company=company, role="DevOps", country="Ireland")


def check(r, e=None, config=CONFIG, register=REGISTER):
    return visa.visa_checks(r, e or Extraction(), REF, config, register)


def test_ind_name_strips_legal_forms_and_country():
    assert ind_name("Tulip Data Nederland B.V.") == "tulip data"
    assert ind_name("Delta Cloud Holding N.V.") == "delta cloud"
    assert ind_name("Windmill Software Netherlands BV") == "windmill software"


@pytest.mark.parametrize("name", ["Delta Cloud", "Delta Cloud B.V.", "Windmill Software",
                                  "Maastricht Logistics"])
def test_register_matches(name):
    assert REGISTER.match(name)


@pytest.mark.parametrize("name", ["Maas Logistics", "Delta", "Windmill", "Acme", ""])
def test_register_near_misses_do_not_match(name):
    assert REGISTER.match(name) is None


def test_target_company_ind_status_wins():
    assert check(nl("Tulip Data B.V.")).flags == [visa.ON_IND]
    assert check(nl("Polder Logistics")).flags == [visa.NOT_ON_IND]


def test_register_lookup_for_other_companies():
    assert check(nl("Delta Cloud")).flags == [visa.ON_IND]
    assert check(nl("Maas Logistics")).flags == [visa.NOT_ON_IND]


def test_register_unavailable_gives_note_not_flag():
    result = check(nl("Delta Cloud"), register=None)
    assert result.flags == []
    assert result.notes == ["IND register unavailable"]


def test_ireland_agency_from_text_or_config():
    agency = Extraction(agency_posting=Quoted(value=True, quote="on behalf of our client"))
    assert check(ie("Some Company"), agency).flags == [visa.AGENCY_IE]
    assert check(ie("Liffey Recruitment Ltd")).flags == [visa.AGENCY_IE]
    assert check(ie("Northwind Cloud")).flags == []
    assert check(ie("Liffey Recruitment"), config=ConfigStore.from_values({})).flags == []


@pytest.mark.parametrize(
    "text, amount",
    [
        ("EUR 45,000 - 55,000 per year", 45000),
        ("60k a year", 60000),
        ("18 000 - 24 000 PLN gross per month", 216000),
        ("EUR 3.500 monthly", 42000),
        ("EUR 40 per hour", None),
        ("competitive salary", None),
        ("50 000", None),
        ("4000 per month or 48000 per year", None),
        (None, None),
    ],
)
def test_annual_amount_is_conservative(text, amount):
    assert visa.annual_amount(text) == amount


def salary(text):
    return Extraction(salary_text=Quoted(value=text, quote=text))


def test_salary_flags_only_with_config_thresholds():
    low = salary("EUR 36,000 per year")
    assert check(ie("Northwind Cloud"), low).flags == []  # no Config keys: never flag
    config = ConfigStore.from_values(
        {"visa.salary_threshold.ireland": "38000", "visa.ie_lower_band_max": "40000"}
    )
    assert check(ie("Northwind Cloud"), low, config).flags == [visa.SALARY_LOW, visa.IE_LOWER_BAND]
    mid = salary("EUR 39,000 per year")
    assert check(ie("Northwind Cloud"), mid, config).flags == [visa.IE_LOWER_BAND]
    high = salary("EUR 65,000 per year")
    assert check(ie("Northwind Cloud"), high, config).flags == []
    vague = salary("EUR 36,000")
    assert check(ie("Northwind Cloud"), vague, config).flags == []
