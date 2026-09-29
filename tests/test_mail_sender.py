"""Mail mode "send": the checks before sending, and sending only what passed."""

import base64
import tempfile
from pathlib import Path

import pytest
from test_telegram_bot import ButtonTelegram, Clock, fake_rendering, settings, update

from jobengine import telegram_bot as tb
from jobengine.mail import preflight, sender
from jobengine.mail.compose import Signature, compose
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY

PDF = b"%PDF-1.7 resume of Alex Example"
SIG = Signature(html="<p>Alex Example<br>+48 555 010 000</p>",
                text="Alex Example\n+48 555 010 000")
BODY = ("Hi Piotr,\n\nI am a DevOps engineer with three years of Terraform and Kubernetes work "
        "and I would like to apply for the DevOps Engineer role at Vistula Cloud.\n\nBest")


def mail(to="me@example.com", subject="[LOCAL] to p@v.example | DevOps role", body=BODY,
         attachment=("Alex_Devops_VistulaCloud.pdf", PDF)):
    return compose(to, subject, body, SIG, attachment)


def expected(**changes):
    base = dict(to="me@example.com", subject="[LOCAL] to p@v.example | DevOps role",
                signature_text=SIG.text, attachment=("Alex_Devops_VistulaCloud.pdf", PDF),
                max_kb=250, env_label="[LOCAL]")
    base.update(changes)
    return preflight.Expected(**base)


def test_a_good_mail_passes_and_its_raw_copy_too():
    m = mail()
    assert preflight.check(m.message, expected()) == []
    assert preflight.check_raw(base64.urlsafe_b64decode(m.raw()), expected()) == []


@pytest.mark.parametrize("change, problem", [
    (lambda m: m.message.replace_header("To", "a@x.example, b@x.example"),
     "To has 2 addresses"),
    (lambda m: m.message.replace_header("To", "someone@else.example"),
     "To is someone@else.example, expected me@example.com"),
    (lambda m: m.message.replace_header("To", "not an address"), "is not a valid address"),
    (lambda m: m.message.__setitem__("Cc", "boss@x.example"), "Cc is set"),
    (lambda m: m.message.__setitem__("Bcc", "boss@x.example"), "Bcc is set"),
    (lambda m: m.message.replace_header("Subject", ""), "the subject is empty"),
    (lambda m: m.message.replace_header("Subject", "[LOCAL] Hello {first_name}"),
     "the subject has a placeholder left"),
    (lambda m: m.message.replace_header("Subject", "DevOps role"),
     "the subject does not start with [LOCAL]"),
])
def test_header_problems(change, problem):
    m = mail()
    change(m)
    assert any(problem in p for p in preflight.check(m.message, expected()))


def test_body_problems():
    problems = preflight.check(mail(body="Hi {first_name}, short").message, expected())
    assert "the plain body has a placeholder left" in problems
    assert any(p.startswith("the body is only") for p in problems)
    unsigned = compose("me@example.com", "[LOCAL] to p@v.example | DevOps role", BODY,
                       Signature(html="<p>Someone Else</p>", text="Someone Else"),
                       ("Alex_Devops_VistulaCloud.pdf", PDF))
    assert "the signature is missing" in preflight.check(unsigned.message, expected())


@pytest.mark.parametrize("attachment, wanted, problem", [
    (("Alex_Devops_OtherCompany.pdf", PDF), None, "expected this job's resume"),
    (("Alex_Devops_VistulaCloud.pdf", b"%PDF-1.7 another resume"), None,
     "not the approved resume of this job"),
    (("Alex_Devops_VistulaCloud.pdf", b"hello"), ("Alex_Devops_VistulaCloud.pdf", b"hello"),
     "the attachment is not a PDF"),
    (None, None, "0 attachments, expected 1 (the resume)"),
    (("Alex_Devops_VistulaCloud.pdf", PDF), "none", "1 attachment(s), expected none"),
])
def test_attachment_problems(attachment, wanted, problem):
    exp = expected() if wanted is None else expected(
        attachment=None if wanted == "none" else wanted)
    problems = preflight.check(mail(attachment=attachment).message, exp)
    assert any(problem in p for p in problems), problems


def test_attachment_over_the_size_limit():
    big = b"%PDF-" + b"0" * 3000
    problems = preflight.check(mail(attachment=("Alex_Devops_VistulaCloud.pdf", big)).message,
                               expected(attachment=("Alex_Devops_VistulaCloud.pdf", big),
                                        max_kb=2))
    assert problems == ["the attachment is 3 KB, over 2 KB"]


def test_unreadable_gmail_copy_is_never_sent():
    assert preflight.check_raw(b"", expected())  # no To, no subject, no body


# ---------------------------------------------------------------- the send flow


def ready_desk(mode="send", dry=False):
    s = settings(DRY_RUN="true" if dry else "false")
    d = fake_desk(s, FAKE_TODAY, now=Clock())
    fake_rendering(d, Path(tempfile.mkdtemp()))
    d.screen()
    d.mailmode_command(mode)
    d.tap("ap:pl-clean")
    log_id = d.resume.resume_log.all_rows()[0][0]
    d.tap("ra:" + log_id.replace("-", ""))
    return d


def statuses(desk):
    return {pid: (v.get("Status"), v.get("Gmail draft ID")) for pid, v in
            desk.mail.contacts.all_rows()}


def test_send_mode_checks_and_sends_every_good_draft():
    d = ready_desk()
    contacts = d.tap("ct:pl-clean")[-1]
    assert contacts.buttons[-1] == ("Check and send mails", "dr:pl-clean")
    assert "Next: tap Check and send mails" in contacts.text
    text = d.tap("dr:pl-clean")[0].text
    assert text.startswith("Mails for Vistula Cloud, DevOps Engineer\nSent 4 of 4 from "
                           "madeshwaranm02 in LOCAL after the checks (To, no Cc or Bcc")
    assert ("- Piotr Example (cold mail): To madeshwaranm02@gmail.com | Subject: [LOCAL] to "
            "piotr.example@vistula.example.com | ") in text
    assert "Attachment: Alex_Devops_VistulaCloud.pdf (0 KB)" in text
    assert "4 of 15 sends used today." in text  # fixture Config mail.daily_send_cap 15
    gmail = d.mail.gmail()
    assert gmail.sent == ["r-fake-draft-1", "r-fake-draft-2", "r-fake-draft-3",
                          "r-fake-draft-4"]
    assert statuses(d)["fake-contact-9"] == ("Contacted", "")
    notes = dict(d.mail.contacts.all_rows())["fake-contact-9"]["Notes"]
    assert f"sent by the bot {FAKE_TODAY.isoformat()} after the checks" in notes
    assert d.state.get(sender.SENT_KEY) == {"date": FAKE_TODAY.isoformat(), "count": 4}


def test_a_draft_that_changed_in_gmail_is_kept():
    d = ready_desk()
    d.tap("ct:pl-clean")
    other = compose("someone@else.example", "x", BODY, SIG, None)
    d.mail.gmail().changed["r-fake-draft-2"] = other.message.as_bytes()
    text = d.tap("dr:pl-clean")[0].text
    assert "Sent 3 of 4" in text
    assert "Kept as drafts, not sent:\n- Rita Example: To is someone@else.example" in text
    assert "r-fake-draft-2" not in d.mail.gmail().sent
    assert "r-fake-draft-2" in d.mail.gmail().open_drafts


def test_daily_cap_keeps_the_rest_as_drafts():
    d = ready_desk()
    d.tap("ct:pl-clean")
    d.state.set(sender.SENT_KEY, {"date": FAKE_TODAY.isoformat(), "count": 14})
    text = d.tap("dr:pl-clean")[0].text
    assert "Sent 1 of 4" in text
    assert "today's 15 sends are used" in text
    assert "15 of 15 sends used today." in text


def test_gmail_refusing_one_keeps_it_as_a_draft():
    d = ready_desk()
    d.tap("ct:pl-clean")
    gmail = d.mail.gmail()
    real = gmail.send_draft

    def refuse(draft_id):
        if draft_id == "r-fake-draft-1":
            raise RuntimeError("quota exceeded")
        return real(draft_id)

    gmail.send_draft = refuse
    text = d.tap("dr:pl-clean")[0].text
    assert "Sent 3 of 4" in text
    assert "Piotr Example: Gmail refused it (quota exceeded)" in text


def test_dry_run_checks_but_sends_nothing():
    d = ready_desk(dry=True)
    d.tap("ct:pl-clean")
    text = d.tap("dr:pl-clean")[0].text
    assert "DRY RUN: 4 of 4 would be sent (every check passed); nothing sent" in text
    assert d.mail.gmail().sent == []
    assert d.state.get(sender.SENT_KEY) is None


def test_draft_mode_never_sends():
    d = ready_desk(mode="draft")
    contacts = d.tap("ct:pl-clean")[-1]
    assert contacts.buttons[-1] == ("Write Gmail drafts", "dr:pl-clean")
    text = d.tap("dr:pl-clean")[0].text
    assert text.startswith("Drafts for Vistula Cloud, DevOps Engineer")
    assert d.mail.gmail().sent == []
    assert len(d.mail.gmail().open_drafts) == 4


def test_mailmode_command():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    fake = ButtonTelegram([[update(1, "/mailmode")], [update(2, "/mailmode send")],
                           [update(3, "/mailmode later")]])
    client = tb.TelegramClient(fake)
    offset = None
    for _ in range(3):
        offset = tb.poll_once(client, settings(), offset, desk=d)
    texts = [t for _, t in fake.sent]
    assert texts[0].startswith("[LOCAL] Mail mode: draft.")
    assert texts[1].startswith("[LOCAL] Mail mode: send.")
    assert texts[2] == ("[LOCAL] Unknown mail mode 'later'. Send /mailmode draft or /mailmode "
                        "send.")
    assert sender.mode(d.state) == sender.SEND
    assert "mailmode" in [c["command"] for c in tb.bot_commands()]


def test_autopilot_in_send_mode():
    s = settings(DRY_RUN="false")
    d = fake_desk(s, FAKE_TODAY, now=Clock())
    fake_rendering(d, Path(tempfile.mkdtemp()))
    d.mailmode_command("send")
    replies = d.autopilot(None)
    summary = replies[0].text
    assert ("2. Vistula Cloud, DevOps Engineer (Apply high): resume saved, 4 contacts, "
            "4 of 4 mails sent") in summary
    assert "Mail mode send: mails that passed every check were sent" in summary
    assert any(r.text.startswith("Mails for Vistula Cloud, DevOps Engineer\nSent 4 of 4")
               for r in replies)
