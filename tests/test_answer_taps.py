"""Buttons answered the moment they arrive (LOG-05, 24 Nov: an Approve tapped while another job
ran was answered late and Telegram said "query is too old and response timeout expired")."""

from test_telegram_bot import settings, tap

from jobengine import telegram_bot as tb
from jobengine.jobctl import CONTROL


class Client:
    def __init__(self, updates=()):
        self.updates = list(updates)
        self.events = []

    def get_updates(self, offset, timeout):
        out, self.updates = self.updates, []
        return out

    def answer_callback(self, callback_id, text=None):
        self.events.append(("answer", callback_id, text))

    def send_message(self, chat_id, text, *args, **kwargs):
        self.events.append(("send", text))

    def typing(self, chat_id):
        pass


def test_a_tap_is_answered_before_it_waits_its_turn():
    client = Client([tap(1, "ap:pl-clean")])
    queued = []

    def dispatch(update):
        client.events.append(("queued", update["update_id"]))
        queued.append(update)

    tb.poll_once(client, settings(), None, dispatch=dispatch)
    assert client.events == [
        ("answer", "cb1", "Checking the job link, then building your resume..."),
        ("queued", 1)]
    assert queued[0]["callback_query"]["_answered"] is True


def test_the_toast_says_when_it_waits_for_a_running_job():
    client = Client()
    with CONTROL.job("/fetch"):
        tb.answer_tap_now(client, settings(), tap(2, "sk:pl-clean"))
    assert client.events == [("answer", "cb2", "Skipping... (after the running /fetch)")]


def test_answered_once_and_only_for_your_chat():
    client = Client()
    update = tap(3, "ap:x")
    tb.answer_tap_now(client, settings(), update)
    tb.answer_tap_now(client, settings(), update)  # already answered
    tb.handle_callback(client, settings(), update["callback_query"], None)  # no second answer
    assert [e for e in client.events if e[0] == "answer"] == [
        ("answer", "cb3", "Checking the job link, then building your resume...")]
    other = Client()
    tb.answer_tap_now(other, settings(), tap(4, "ap:x", user="999"))
    tb.answer_tap_now(other, settings(), {"update_id": 5, "message": {"text": "/help"}})
    assert other.events == []


def test_a_tap_handled_at_once_is_still_answered_there():
    client = Client()
    tb.handle_callback(client, settings(), tap(6, "nx:1")["callback_query"], None)
    assert client.events[0] == ("answer", "cb6", tb.TAP_TOASTS["nx"])
