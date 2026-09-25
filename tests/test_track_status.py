import pytest

from jobengine.track.models import (
    ACK,
    FOLLOWUP_SENT,
    INTERVIEW,
    NEUTRAL,
    OFFER,
    OPT_OUT,
    POSITIVE,
    REJECTION,
    SCREENING,
)
from jobengine.track.status import JOB_TERMINAL, Effect, advance_contact, advance_job, effect


def test_forward_only():
    assert advance_job("Applied", "Screening") == "Screening"
    assert advance_job("Interview", "Screening") is None
    assert advance_job("Interview", "Offer") == "Offer"
    assert advance_job("Offer", "Interview") is None
    assert advance_job("Screening", "Replied") is None


@pytest.mark.parametrize("terminal", JOB_TERMINAL)
def test_terminal_is_never_changed(terminal):
    for target in ("Replied", "Interview", "Offer", "Rejected", "Ghosted"):
        assert advance_job(terminal, target) is None


def test_terminal_from_non_terminal():
    assert advance_job("Interview", "Rejected") == "Rejected"
    assert advance_job("Applied", "Ghosted") == "Ghosted"


def test_replied_only_from_applied_or_followed_up():
    assert advance_job("Applied", "Replied") == "Replied"
    assert advance_job("Followed up", "Replied") == "Replied"
    assert advance_job("Resume built", "Replied") is None
    assert advance_job("Resume built", "Followed up") is None
    assert advance_job("Applied", "Followed up") == "Followed up"


def test_effects_table():
    assert effect(POSITIVE, "Applied", "Contacted") == Effect("Replied", "Replied", True)
    assert effect(NEUTRAL, "Resume built", "Contacted") == Effect(None, "Replied", True)
    assert effect(SCREENING, "Applied", "Followed up").job_status == "Screening"
    assert effect(INTERVIEW, "Screening", "Replied").job_status == "Interview"
    assert effect(OFFER, "Interview", "Replied").job_status == "Offer"
    assert effect(REJECTION, "Interview", "Replied").job_status == "Rejected"
    assert effect(OPT_OUT, "Applied", "Contacted") == Effect(None, "Do not contact", True)
    assert effect(ACK, "Applied", None, cold_mail=False) == Effect(None, None, True)
    followup = Effect("Followed up", "Followed up", True)
    assert effect(FOLLOWUP_SENT, "Applied", "Contacted") == followup
    assert effect(REJECTION, "Rejected", "Replied").touch is False
    assert effect("other", "Applied", "Contacted").touch is False


def test_contact_rules():
    assert advance_contact("Contacted", "Replied") == "Replied"
    assert advance_contact("Ghosted", "Replied") == "Replied"
    assert advance_contact("Replied", "Contacted") is None
    assert advance_contact("Do not contact", "Replied") is None
    assert advance_contact("Bounced", "Contacted") is None
    assert advance_contact("Contacted", "Ghosted") is None
    assert advance_contact("Followed up", "Ghosted") == "Ghosted"
    assert advance_contact("Replied", "Do not contact") == "Do not contact"
