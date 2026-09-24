from datetime import date, timedelta

from jobengine.sweep.ghost import ghost_risk

TODAY = date(2026, 10, 1)


def ago(days):
    return TODAY - timedelta(days=days)


def test_high_needs_three_sightings_over_sixty_days():
    assert ghost_risk(3, ago(60), None, TODAY) == "High"
    assert ghost_risk(3, ago(59), None, TODAY) == "Medium"  # still 2+ times over 30 days
    assert ghost_risk(2, ago(90), None, TODAY) == "Medium"


def test_medium_by_repeat_sightings():
    assert ghost_risk(2, ago(30), None, TODAY) == "Medium"
    assert ghost_risk(2, ago(29), None, TODAY) == "Unknown"


def test_medium_by_old_posted_date():
    assert ghost_risk(1, TODAY, ago(46), TODAY) == "Medium"
    assert ghost_risk(1, TODAY, ago(45), TODAY) == "Low"


def test_low_when_posted_date_known():
    assert ghost_risk(1, TODAY, ago(3), TODAY) == "Low"


def test_unknown_first_sighting_without_posted_date():
    assert ghost_risk(1, TODAY, None, TODAY) == "Unknown"
