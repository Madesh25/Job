from datetime import UTC, datetime

from jobengine.track.bounces import failed_recipients, is_bounce
from jobengine.track.models import Message

WHEN = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


def test_from_header():
    m = Message("m1", "t1", WHEN, "mailer-daemon@googlemail.com",
                headers={"x-failed-recipients": "Bob.Example@vistula.example.com, x@example.org"})
    assert is_bounce(m)
    assert failed_recipients(m) == ["bob.example@vistula.example.com", "x@example.org"]


def test_from_dsn_body():
    body = ("Reporting-MTA: dns; mx.example.org\n\n"
            "Final-Recipient: rfc822; olga.example@northwind.example.org\n"
            "Action: failed\nStatus: 5.1.1\n")
    m = Message("m2", "t2", WHEN, "postmaster@northwind.example.org", text=body)
    assert is_bounce(m)
    assert failed_recipients(m) == ["olga.example@northwind.example.org"]


def test_normal_mail_is_not_a_bounce():
    m = Message("m3", "t3", WHEN, "ola.example@vistula.example.com", text="Thanks!")
    assert not is_bounce(m)
    assert failed_recipients(m) == []
