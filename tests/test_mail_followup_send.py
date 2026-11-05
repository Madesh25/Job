"""5 Nov (the autopilot timetable): in mail mode send the bot sends the follow-ups too, in
the recipient's morning and only Tuesday to Thursday, instead of a draft you send at night."""

from datetime import datetime
from zoneinfo import ZoneInfo

from test_mail_sender import ready_desk
from test_track_runner import NOW, deps, run

from jobengine.config_store import ConfigStore
from jobengine.mail import sender, timing
from jobengine.mail.compose import compose, signature

WARSAW = ZoneInfo("Europe/Warsaw")


def test_the_daily_check_queues_the_follow_ups_in_mail_mode_send():
    d = deps()
    d.state.set(sender.MODE_KEY, {"mode": sender.SEND})
    report = run(d)
    assert report.followups == []
    # NOW is Thursday 15 Oct, 04:30 in Warsaw: a follow-up day, so today at 08:00.
    assert sorted(report.followups_queued) == ["Anna Example (Thu 15 Oct 08:00 CEST)",
                                               "Tomek Example (Thu 15 Oct 08:00 CEST)"]
    items = timing.load(d.state)
    assert {i.template for i in items} == {"followup"} and len(items) == 2
    assert "Follow-ups queued, sent by the bot in the recipient's morning (Tue to Thu" in \
        report.text()
    # Config off: drafts you send yourself, as before.
    d = deps()
    d.state.set(sender.MODE_KEY, {"mode": sender.SEND})
    d.config = lambda: ConfigStore.from_values({**ConfigStore.fake().all(),
                                                 sender.FOLLOWUP_KEY: "off"})
    report = run(d, NOW)
    assert sorted(report.followups) == ["Anna Example", "Tomek Example"]
    assert timing.load(d.state) == []
    # Draft mode: never queued.
    d = deps()
    assert timing.load(d.state) == [] and run(d).followups_queued == []


def test_a_follow_up_waits_for_tuesday_and_goes_without_an_attachment():
    w = timing.window({}, ConfigStore.from_values({}))
    follow = timing.for_template(w, "followup")
    assert follow.days == (1, 2, 3) and timing.for_template(w, "peer") is w
    friday = datetime(2026, 10, 2, 11, 0, tzinfo=WARSAW)
    assert timing.next_start(follow, "Poland", friday) == datetime(2026, 10, 6, 8, 0,
                                                                   tzinfo=WARSAW)

    d = ready_desk()
    d.tap("ct:pl-clean")
    config = d.mail.config()
    gmail = d.mail.gmail()
    page_id, row = next((pid, v) for pid, v in d.mail.contacts.all_rows()
                        if (v.get("Name") or "") == "Piotr Example")
    sig = signature(config)
    subject = "[LOCAL] to piotr.example@vistula.example.com | Re: DevOps Engineer role"
    mail = compose("madeshwaranm02@gmail.com", subject, "Hi Piotr,\n\nJust following up.",
                   sig, None)
    draft = gmail.create_draft(mail.raw(), thread_id="t-piotr")
    before = dict(row)
    d.mail.now = lambda: friday
    when = sender.queue_followup(
        d.s, config, d.state, friday, draft_id=draft.draft_id, job_id="pl-clean",
        page_id=page_id, name="Piotr Example", to="madeshwaranm02@gmail.com",
        subject=subject, thread_id="t-piotr", country="Poland",
        signature=sig.text.strip().splitlines()[0])
    assert when == "Tue 06 Oct 08:00 CEST"
    d.mail.now = lambda: datetime(2026, 10, 5, 8, 30, tzinfo=WARSAW)  # Monday: not yet
    assert d.mail_queue_tick() == []
    d.mail.now = lambda: datetime(2026, 10, 6, 8, 30, tzinfo=WARSAW)  # Tuesday
    text = d.mail_queue_tick()[0].text
    assert text.startswith("Sent at 08:30 CEST (the recipient's morning), after the checks: "
                           "Piotr Example (follow-up)")
    after = dict(d.mail.contacts.all_rows())[page_id]
    assert after.get("Status") == before.get("Status")  # the daily check records it
