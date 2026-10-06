"""/end stops a long run at its next step, /restart starts the bot again (6 Oct). The real
bot answers on a background thread, so it keeps reading messages while /fetch runs."""

import threading
import time

from jobengine import telegram_bot as tb
from jobengine.jobctl import Control, Stopped, end_text
from jobengine.settings import load_settings

CHAT_ID = "111222333"
S = load_settings("local", {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": CHAT_ID})


def update(update_id, text, date=None):
    message = {"chat": {"id": int(CHAT_ID)}, "text": text}
    if date is not None:
        message["date"] = date
    return {"update_id": update_id, "message": message}


class FakeTelegram:
    """Update batches in, sent texts recorded. A batch waits for `gate` (if set) first, so
    /end arrives while the run is going."""

    def __init__(self, batches, gate=None):
        self.batches, self.gate = list(batches), gate
        self.sent, self.polls = [], []

    def __call__(self, method, payload, timeout):
        if method == "getUpdates":
            self.polls.append((payload.get("offset"), payload["timeout"]))
            if self.batches and self.gate is not None and self.gate[0] == len(self.polls):
                self.gate[1].wait(5)
            return {"ok": True, "result": self.batches.pop(0) if self.batches else []}
        if method == "sendMessage":
            self.sent.append(payload["text"])
        return {"ok": True, "result": {}}


def test_end_with_nothing_running_and_stopped_is_not_an_exception():
    control = Control()
    assert end_text(control) == "Nothing is running."
    assert tb.reply_for("/end", S) == "Nothing is running."
    with control.job("/fetch"):
        assert end_text(control).startswith("Stopping /fetch (started ")
        try:
            control.check()
        except Exception:  # noqa: BLE001 (what a source's error handler would do)
            raise AssertionError("Stopped must pass through except Exception") from None
        except Stopped:
            pass
    control.check()  # the next run starts clean


def test_end_stops_a_running_fetch_and_later_messages_wait():
    started, steps = threading.Event(), []

    def fetch(progress):
        started.set()
        for step in range(500):
            progress(f"step {step}")
            steps.append(step)
            time.sleep(0.01)
        return "Sweep done"

    fake = FakeTelegram([[update(1, "/fetch")], [update(2, "/help")], [update(3, "/end")]],
                        gate=(2, started))
    tb.run(S, tb.TelegramClient(fake), max_polls=4, fetch=fetch, background=True)
    texts = "\n".join(fake.sent)
    assert "Waiting: this runs after /fetch (/end stops /fetch)." in texts
    assert "Stopping /fetch (started " in texts
    assert "/fetch stopped by /end after under a minute. What was done before it stopped " \
        "is kept." in texts
    assert "Sweep done" not in texts and len(steps) < 500
    assert "Job Engine commands" in fake.sent[-1]  # the waiting /help ran afterwards


def test_restart_confirms_the_message_then_restarts():
    restarted = []
    fake = FakeTelegram([[update(7, "/restart")]])
    tb.run(S, tb.TelegramClient(fake), max_polls=3, background=True,
           restart=lambda: restarted.append(1))
    assert restarted == [1]
    assert fake.polls[-1] == (8, 0)  # confirmed, so Telegram does not deliver it again
    assert fake.sent[-1] == "[LOCAL] Restarting the bot..."


def test_a_restart_from_before_the_start_is_not_done_again():
    fake = FakeTelegram([[update(7, "/restart", date=1_000)]])
    offset = tb.poll_once(tb.TelegramClient(fake), S, None, started=1_500,
                          dispatch=lambda u: None)
    assert offset == 8 and fake.sent == []


def test_the_webhook_answers_end_before_the_queue():
    fake = FakeTelegram([])
    client = tb.TelegramClient(fake)
    assert tb.end_now(client, S, update(1, "/end"))
    assert fake.sent == ["[LOCAL] Nothing is running."]
    assert not tb.end_now(client, S, update(2, "/fetch"))
    other = {"update_id": 3, "message": {"chat": {"id": 5}, "text": "/end"}}
    assert not tb.end_now(client, S, other)
