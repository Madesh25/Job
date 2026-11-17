from datetime import date

import pytest

from jobengine.config_store import ConfigStore
from jobengine.contacts.credits import Counter, CreditBook, monthly_reset, parse_counter
from jobengine.safety import SafetyError, check_config_write

TODAY = date(2026, 10, 1)


def test_parse_and_format():
    counter = parse_counter("12 / 75 per month")
    assert (counter.used, counter.limit, counter.left) == (12, 75, 63)
    assert counter.text() == "12 / 75 per month" and counter.short() == "12/75"
    assert parse_counter("DISABLED") is None
    assert parse_counter(None) is None


def test_monthly_reset():
    old = Counter(used=20, limit=25, updated=date(2026, 9, 30))
    assert monthly_reset(old, TODAY).used == 0
    same = Counter(used=20, limit=25, updated=date(2026, 10, 1))
    assert monthly_reset(same, TODAY).used == 20
    never = Counter(used=5, limit=25, updated=None)
    assert monthly_reset(never, TODAY).used == 5


def test_credit_book_from_fake_config_resets_last_month_and_simulates():
    book = CreditBook(ConfigStore.fake(), TODAY)
    # Fake config: apollo 12/75 (Sep 28), hunter 3/25 (Sep 15), snov 0/50 (Aug 20).
    assert book.available("apollo") == 75 and book.available("hunter") == 25
    book.spend("hunter", 2)
    assert book.counters["hunter"].used == 2
    assert book.line() == "Credits: Apollo 0/75, Hunter 2/25, Snov 0/50"


def test_used_up_counter_has_nothing_available():
    config = ConfigStore.from_values({"credits.hunter": "25 / 25 per month"})
    book = CreditBook(config, TODAY)
    assert book.available("hunter") == 0
    assert book.available("snov") == 0  # missing row: never spend


def test_writer_is_called_with_allowlisted_keys_only():
    writes = []
    book = CreditBook(ConfigStore.from_values({"credits.apollo": "1 / 75 per month"}), TODAY,
                      writer=lambda key, value, day: writes.append((key, value, day)))
    book.spend("apollo", 2)
    assert writes == [("credits.apollo", "3 / 75 per month", TODAY)]


def test_config_key_allowlist():
    check_config_write("credits.hunter")
    with pytest.raises(SafetyError):
        check_config_write("model.tailor")
