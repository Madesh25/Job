"""5 Nov (the autopilot timetable): you apply the same night, so a job's mails wait in the
queue until you applied, then go out in the recipient's next morning."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from test_mail_sender import ready_desk

from jobengine.config_store import ConfigStore
from jobengine.mail import sender, timing

WARSAW = ZoneInfo("Europe/Warsaw")


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=WARSAW)


def held_desk():
    d = ready_desk(applied=False)
    d.mail.now = lambda: at(1, 9)  # Thursday 09:00 in Warsaw: inside the window
    d.tap("ct:pl-clean")
    return d


def test_mails_wait_until_you_applied_even_inside_the_window():
    d = held_desk()
    text = d.tap("dr:pl-clean")[0].text
    assert "Sent 0 of 4" in text
    assert ("Waiting until you apply (tap I applied under the resume; the employer's "
            "thank-you mail counts too), then sent in the recipient's morning after the same "
            "checks:\n- Piotr Example (cold mail)") in text
    items = timing.load(d.state)
    assert len(items) == 4 and {i.hold for i in items} == {sender.HOLD}
    assert d.mail_queue_tick() == []  # not applied yet: nothing goes
    queue = d.mailqueue_command()[0].text
    assert "- Piotr Example (cold mail), after you apply: " in queue


def test_after_i_applied_they_go_in_the_recipients_morning():
    d = held_desk()
    d.tap("dr:pl-clean")
    d.tap("ia:pl-clean")
    d.mail.now = lambda: at(1, 20)  # Thursday evening: the window is closed
    assert d.mail_queue_tick() == []
    d.mail.now = lambda: at(2, 8, 10)  # Friday morning
    first = d.mail_queue_tick()[0].text
    assert first.startswith("Sent at 08:10 CEST (the recipient's morning), after the checks:")
    assert "1 of 10 sends used today. 3 mails wait in the queue." in first
    assert not any(i.hold for i in timing.load(d.state))


def test_a_held_mail_leaves_the_queue_when_the_job_moved_on_or_you_did_not_apply():
    d = held_desk()
    d.tap("dr:pl-clean")
    d.mail.jobs.update("pl-clean", {"Status": "Declined"})
    text = d.mail_queue_tick()[0].text
    assert text.startswith("Left the mail queue, kept as drafts (the job is Declined): "
                           "Piotr Example")
    assert timing.load(d.state) == []

    d = held_desk()
    d.tap("dr:pl-clean")
    later = at(1, 9) + timedelta(days=sender.HOLD_DAYS + 1)  # a Friday, inside the window
    d.mail.now = lambda: later
    text = d.mail_queue_tick()[0].text
    assert text.startswith(f"Left the mail queue, kept as drafts (not applied within "
                           f"{sender.HOLD_DAYS} days): ")
    assert timing.load(d.state) == []


def test_config_off_sends_before_you_applied():
    d = held_desk()
    store = ConfigStore.from_values({**ConfigStore.fake().all(), sender.AFTER_KEY: "off"})
    d.mail.config = lambda: store
    assert "Sent 4 of 4" in d.tap("dr:pl-clean")[0].text
    assert timing.load(d.state) == []
