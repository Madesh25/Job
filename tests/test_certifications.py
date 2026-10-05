"""Certifications table (5 Oct): only Passed and ticked certifications reach the resume."""

from datetime import date

from jobengine.resume.sections import SectionRow, certification_lines, with_certifications

CERTS = [
    {"Name": "Certified Kubernetes Administrator (CKA)", "Issuer": "The Linux Foundation",
     "Status": "Passed", "Passed": date(2026, 11, 20), "Show on resume": True, "Order": 1},
    {"Name": "Microsoft Certified: Azure Fundamentals (AZ-900)", "Issuer": "Microsoft",
     "Status": "Studying", "Show on resume": True, "Order": 2},
    {"Name": "Microsoft Certified: DevOps Engineer Expert (AZ-400)", "Issuer": "Microsoft",
     "Status": "Passed", "Passed": date(2027, 2, 1), "Show on resume": False, "Order": 3},
]


def test_only_passed_and_ticked_certifications_are_listed():
    assert certification_lines(CERTS) == (
        "Certified Kubernetes Administrator (CKA), The Linux Foundation, November 2026")


def test_the_table_replaces_the_resume_sections_row():
    old = SectionRow("Certifications", False, 5.0, ("Poland",), "<Month Year>")
    other = SectionRow("Languages", True, 6.0, ("All",), "English")
    rows = with_certifications([old, other], CERTS)
    cert = next(r for r in rows if r.kind == "certifications")
    assert cert.enabled and cert.countries == ("All",) and cert.order == 5.0
    assert other in rows
    none = with_certifications([old], [{**CERTS[1]}])  # still studying: nothing shown
    assert not next(r for r in none if r.kind == "certifications").enabled
