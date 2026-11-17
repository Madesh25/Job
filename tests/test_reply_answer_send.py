"""5 Nov (the autopilot timetable): the draft answer under a reply ping can go in the
recipient's morning (a Gmail draft you can still edit) or at once, after the same checks."""

from datetime import datetime
from zoneinfo import ZoneInfo

from test_reply_ping import make_desk
from test_telegram_bot import settings

from jobengine.mail import timing

WARSAW = ZoneInfo("Europe/Warsaw")


def piotr_ping(d):
    replies = d.reply_ping_tick()
    return next(r for r in replies if "Piotr Example" in r.text)


def test_the_ping_offers_their_morning_or_now():
    ping = piotr_ping(make_desk(settings(DRY_RUN="false")))
    labels = [label for label, _data in ping.buttons]
    assert labels == ["✉️ Send in their morning", "Send now"]
    assert ping.buttons[0][1].startswith("ym:") and ping.buttons[1][1].startswith("yn:")


def test_their_morning_queues_a_gmail_draft_that_goes_after_the_checks():
    d = make_desk(settings(DRY_RUN="false"))
    key = piotr_ping(d).buttons[0][1]
    d.mail.now = lambda: datetime(2026, 10, 1, 20, 0, tzinfo=WARSAW)  # Thursday evening
    text = d.tap(key)[0].text
    assert text.startswith("Your answer to Piotr Example is a Gmail draft in the same thread "
                           "(edit it there if you like). It goes Fri 02 Oct 08:00 CEST")
    items = timing.load(d.state)
    assert [(i.template, i.rank) for i in items] == [("reply", "0")]
    assert d.tap(key)[0].text.startswith("This draft answer is no longer kept")
    d.mail.now = lambda: datetime(2026, 10, 2, 8, 5, tzinfo=WARSAW)
    sent = d.mail_queue_tick()[0].text
    assert sent.startswith("Sent at 08:05 CEST (the recipient's morning), after the checks: "
                           "Piotr Example (answer)")
    assert timing.load(d.state) == []


def test_send_now_and_dry_run():
    d = make_desk(settings(DRY_RUN="false"))
    key = piotr_ping(d).buttons[1][1]
    d.mail.now = lambda: datetime(2026, 10, 1, 20, 0, tzinfo=WARSAW)
    assert d.tap(key)[0].text.startswith("Sent at 20:00 CEST (now, as you asked), after the "
                                         "checks: Piotr Example (answer)")
    d = make_desk()
    key = piotr_ping(d).buttons[0][1]
    assert d.tap(key)[0].text.startswith("DRY RUN: would write your answer to Piotr Example")
