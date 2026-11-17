"""Clean inbox (C2, 26 Oct): alert mails with a job are archived and marked read; nothing
else is touched. Made-up mails."""

from test_telegram_bot import settings

from jobengine.config_store import ConfigStore
from jobengine.gmail_reader import GmailMessage
from jobengine.sweep import fakes, inbox_tidy
from jobengine.sweep.sources import gmail_alerts


class Client:
    def __init__(self, labels, refuse_after=None):
        self._labels, self.refuse_after, self.calls = labels, refuse_after, []

    def labels(self):
        return self._labels

    def modify_labels(self, message_id, add, remove):
        if self.refuse_after is not None and len(self.calls) >= self.refuse_after:
            raise RuntimeError("HttpError 403: Request had insufficient authentication scopes")
        self.calls.append((message_id, add, remove))


def test_only_mails_with_a_job_are_kept_for_the_tidy():
    recruiter = GmailMessage(id="inmail", sender="LinkedIn <messages-noreply@linkedin.com>",
                             subject="Anna sent you a message", html="<p>Hi, are you open?</p>")
    messages = [*fakes.gmail_messages(), recruiter]
    result = gmail_alerts.fetch(settings(), load_messages=lambda: messages)
    ids = [m[0] for m in result.alert_mails[""]]
    assert ids and "inmail" not in ids
    assert len(ids) == len({m.id for m in messages if gmail_alerts.parse_alert(
        m, settings().sweep.get("gmail") or {})})


def test_archived_marked_read_and_labelled_when_the_label_exists():
    m02 = Client({"Job Alerts": "L1", "Job Alerts/LinkedIn": "L2"})
    mm = Client({"Job Alerts": "L9"})
    clients = {"t-m02": m02, "t-mm": mm}
    found = {"m02": [("a", "LinkedIn", False), ("b", "Pracuj.pl", True)],
             "mm": [("c", "LinkedIn", False)]}
    lines = inbox_tidy.tidy({"m02": "t-m02", "mm": "t-mm"}, found, "Job Alerts",
                            client_for=clients.__getitem__)
    assert m02.calls == [("a", ["L1", "L2"], ["INBOX", "UNREAD"]),
                         ("b", ["L1"], ["INBOX", "UNREAD", "SPAM"])]
    assert mm.calls == [("c", ["L9"], ["INBOX", "UNREAD"])]
    assert lines == ["Inbox tidy: 3 alert mail(s) archived and marked read (m02 2, mm 1); they "
                     "keep the Job Alerts label."]


def test_a_token_without_gmail_modify_is_named_and_the_other_mailbox_goes_on():
    m02 = Client({"Job Alerts": "L1"}, refuse_after=0)
    mm = Client({})
    clients = {"t-m02": m02, "t-mm": mm}
    lines = inbox_tidy.tidy({"m02": "t-m02", "mm": "t-mm"},
                            {"m02": [("a", "LinkedIn", False)],
                             "mm": [("c", "IrishJobs.ie", False)]},
                            "Job Alerts", client_for=clients.__getitem__)
    assert lines[0].startswith("Inbox tidy: 1 alert mail(s) archived and marked read (mm 1)")
    assert lines[1] == ("Inbox tidy stopped for m02 after 0: its token cannot change mail "
                        "(renew it with /gmailtoken to add gmail.modify)")
    assert mm.calls == [("c", [], ["INBOX", "UNREAD"])]


def test_off_unless_config_says_true():
    assert not inbox_tidy.enabled(ConfigStore.fake())
    assert inbox_tidy.enabled(ConfigStore.from_values({"alerts.tidy": "True"}))


def test_the_fetch_summary_shows_it():
    from jobengine.sweep.models import SweepSummary

    text = SweepSummary(tidy=["Inbox tidy: 2 alert mail(s) archived"],
                        alert_emails=2).friendly_text()
    assert "\U0001F9F9 Inbox tidy: 2 alert mail(s) archived" in text
