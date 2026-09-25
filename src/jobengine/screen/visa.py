"""Country rules (spec section 7, V16 section 13): Visa flags from facts only.

Salary thresholds come from Config (`visa.salary_threshold.<country>`,
`visa.ie_lower_band_max`) and are never hardcoded: when a key is missing nothing is flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobengine.config_store import ConfigStore
from jobengine.ind_register import IndRegister
from jobengine.reference import Reference, company_key
from jobengine.screen.models import Extraction, JobRow

ON_IND = "On IND register"
NOT_ON_IND = "Not on IND register"
AGENCY_IE = "Agency posting (IE)"
SALARY_LOW = "Salary below visa minimum"
IE_LOWER_BAND = "IE lower band - degree risk"
SPONSORSHIP_STATED = "Sponsorship stated"

YEAR_WORDS = r"(per\s+year|per\s+annum|p\.?\s?a\.?|a\s+year|/\s*year|/\s*yr|yearly|annual(?:ly)?)"
MONTH_WORDS = r"(per\s+month|a\s+month|/\s*month|/\s*mo\b|monthly|mies|miesi)"
OTHER_PERIODS = r"(per\s+(hour|day|week)|/\s*(h|hour|day|week)\b|hourly|daily|weekly|godz)"
AMOUNT = re.compile(r"(\d{1,3}(?:[ ,. ]\d{3})+|\d+(?:\.\d+)?)\s*(k\b)?", re.IGNORECASE)


@dataclass
class VisaResult:
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)


def _amounts(text: str) -> list[float]:
    out = []
    for number, k in AMOUNT.findall(text):
        digits = re.sub(r"[ ,. ](?=\d{3}\b)", "", number)
        try:
            value = float(digits)
        except ValueError:
            continue
        out.append(value * 1000 if k else value)
    return out


def annual_amount(text: str | None) -> float | None:
    """Lowest yearly amount in a salary text, or None when the text is ambiguous.

    Needs an explicit period (per year or per month). Hourly, daily or weekly pay, both
    periods at once, or no number at all count as ambiguous.
    """
    if not text:
        return None
    low = text.lower()
    yearly = re.search(YEAR_WORDS, low) is not None
    monthly = re.search(MONTH_WORDS, low) is not None
    if yearly == monthly or re.search(OTHER_PERIODS, low):
        return None
    amounts = [a for a in _amounts(low) if a >= 100]
    if not amounts:
        return None
    lowest = min(amounts)
    return lowest * 12 if monthly else lowest


def config_amount(config: ConfigStore, key: str) -> float | None:
    """A threshold from Config. A bare number is a yearly amount."""
    value = config.get(key)
    if not value:
        return None
    if re.search(MONTH_WORDS, value.lower()):
        return annual_amount(value)
    amounts = [a for a in _amounts(value.lower()) if a >= 100]
    return min(amounts) if amounts else None


def _agency_names(config: ConfigStore) -> set[str]:
    raw = config.get("ireland.agency_names") or ""
    return {company_key(part) for part in raw.split(",") if part.strip()}


def visa_checks(
    row: JobRow,
    ext: Extraction,
    ref: Reference,
    config: ConfigStore,
    register: IndRegister | None,
) -> VisaResult:
    """Visa flags for one row. `register` None means the IND register could not be loaded."""
    result = VisaResult()
    country = row.country or ""
    if country == "Netherlands":
        company = ref.company(row.company)
        sponsor = company.ind_sponsor if company else None
        if sponsor == "Verified":
            result.add(ON_IND)
        elif sponsor == "Not listed":
            result.add(NOT_ON_IND)
        elif register is None:
            result.notes.append("IND register unavailable")
        elif register.match(row.company):
            result.add(ON_IND)
        else:
            result.add(NOT_ON_IND)
    if country == "Ireland":
        if ext.agency_posting.value is True or company_key(row.company) in _agency_names(config):
            result.add(AGENCY_IE)

    salary = annual_amount(ext.salary_text.value)
    threshold = config_amount(config, f"visa.salary_threshold.{country.lower()}")
    if salary is not None and threshold is not None and salary < threshold:
        result.add(SALARY_LOW)
    if country == "Ireland" and salary is not None:
        lower_band = config_amount(config, "visa.ie_lower_band_max")
        if lower_band is not None and salary <= lower_band:
            result.add(IE_LOWER_BAND)
    return result
