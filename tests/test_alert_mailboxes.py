"""Two job-alert mailboxes (6 Oct): m02 and mm are both read, one failing does not stop the
other, and the old GMAIL_ALERTS_TOKEN_JSON still works as m02."""

from jobengine.gmail_reader import GmailMessage
from jobengine.settings import load_settings
from jobengine.sweep.sources import gmail_alerts

SENDER = "LinkedIn <jobalerts-noreply@linkedin.com>"


def card(job_id: str) -> str:
    return (f'<div><a href="https://www.linkedin.com/comm/jobs/view/{job_id}">DevOps Engineer'
            "</a><div>Acme</div><div>Dublin</div></div>")


def settings(**tokens):
    return load_settings("dev", tokens)


def fake_boxes(monkeypatch, boxes):
    """boxes: token -> messages, or an exception to raise."""
    def loader(token, gmail_cfg):
        def load():
            if isinstance(boxes[token], Exception):
                raise boxes[token]
            return boxes[token]
        return load

    monkeypatch.setattr(gmail_alerts, "_loader", loader)


def test_mailboxes_old_name_and_same_token_once():
    assert gmail_alerts.mailboxes(settings()) == []
    assert gmail_alerts.mailboxes(settings(GMAIL_ALERTS_TOKEN_JSON="a")) == [("m02", "a")]
    both = settings(GMAIL_ALERTS_TOKEN_JSON_m02="a", GMAIL_ALERTS_TOKEN_JSON_mm="b",
                    GMAIL_ALERTS_TOKEN_JSON="old")
    assert gmail_alerts.mailboxes(both) == [("m02", "a"), ("mm", "b")]
    same = settings(GMAIL_ALERTS_TOKEN_JSON_m02="a", GMAIL_ALERTS_TOKEN_JSON_mm="a")
    assert gmail_alerts.mailboxes(same) == [("m02", "a")]


def test_both_mailboxes_are_read(monkeypatch):
    fake_boxes(monkeypatch, {"a": [GmailMessage("1", SENDER, "jobs", card("111"))],
                             "b": [GmailMessage("2", SENDER, "jobs", card("222")),
                                   GmailMessage("3", SENDER, "jobs", card("111"))]})
    s = settings(GMAIL_ALERTS_TOKEN_JSON_m02="a", GMAIL_ALERTS_TOKEN_JSON_mm="b")
    result = gmail_alerts.fetch(s)
    assert result.emails == 3
    assert sorted(p.posting_id for p in result.postings) == ["111", "222"]  # one per job
    text = gmail_alerts.check(s)
    assert "Mailboxes: m02 1, mm 2" in text and "- [mm] " in text


def test_one_mailbox_failing_keeps_the_other(monkeypatch):
    fake_boxes(monkeypatch, {"a": [GmailMessage("1", SENDER, "jobs", card("111"))],
                             "b": RuntimeError("token expired")})
    s = settings(GMAIL_ALERTS_TOKEN_JSON_m02="a", GMAIL_ALERTS_TOKEN_JSON_mm="b")
    result = gmail_alerts.fetch(s)
    assert result.skipped_reason is None and len(result.postings) == 1
    assert result.notes == ["gmail mailbox mm: RuntimeError: token expired"]
    assert "Mailbox mm: RuntimeError: token expired (failed)" in gmail_alerts.check(s)

    fake_boxes(monkeypatch, {"a": RuntimeError("x"), "b": RuntimeError("y")})
    assert gmail_alerts.fetch(s).skipped_reason == \
        "gmail failed: m02: RuntimeError: x; mm: RuntimeError: y"
