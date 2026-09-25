import pytest

from jobengine.deploy import set_webhook as sw
from jobengine.settings import load_settings

SECRET = "a" * 16 + "B1" * 8


def settings(**extra):
    return load_settings("dev", {"TELEGRAM_BOT_TOKEN": "123:fake",
                                 "TELEGRAM_WEBHOOK_SECRET": SECRET, **extra})


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, payload, timeout):
        self.calls.append((method, payload))
        if method == "getWebhookInfo":
            return {"ok": True, "result": {"url": "https://x.a.run.app/telegram/webhook",
                                           "pending_update_count": 0, "ip_address": "1.2.3.4"}}
        return {"ok": True, "result": True, "description": "Webhook was set"}


def test_set_webhook_payload(capsys):
    rec = Recorder()
    assert sw.main(["--url", "https://x.a.run.app/"], lambda: settings(), rec) == 0
    method, payload = rec.calls[0]
    assert method == "setWebhook"
    assert payload == {"url": "https://x.a.run.app/telegram/webhook", "secret_token": SECRET,
                       "allowed_updates": ["message", "callback_query"],
                       "drop_pending_updates": True}
    out = capsys.readouterr().out
    assert SECRET not in out and "pending_update_count" in out and "ip_address" not in out


def test_delete_webhook():
    rec = Recorder()
    assert sw.main(["--delete"], lambda: settings(), rec) == 0
    assert rec.calls[0][0] == "deleteWebhook"


@pytest.mark.parametrize("secret", ["", "short", "has spaces but is long enough 1234567890"])
def test_weak_or_missing_secret_is_refused(secret):
    rec = Recorder()
    code = sw.main(["--url", "https://x.a.run.app"],
                   lambda: settings(TELEGRAM_WEBHOOK_SECRET=secret), rec)
    assert code == 1 and rec.calls == []


def test_http_url_is_refused():
    with pytest.raises(ValueError):
        sw.webhook_url("http://x.a.run.app")
