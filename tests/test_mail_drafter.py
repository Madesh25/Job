import base64
import dataclasses
from datetime import date
from email import message_from_bytes, policy

import pytest

from jobengine.config_store import ConfigStore
from jobengine.gmail_client import NO_MODIFY, FakeGmail, GmailClient
from jobengine.mail import drafter
from jobengine.mail.templates import fake_templates
from jobengine.notion_repo import FakeResumeLogRepo
from jobengine.settings import load_settings

TODAY = date(2026, 9, 30)
LONG_DASH = chr(0x2014)


def settings(env="dev", dry="false", **extra):
    return load_settings(env, {"DRY_RUN": dry, **extra})


def deps_for(s, tmp_path, config=None, **kw):
    deps = drafter.fake_deps(s, root=tmp_path, **kw)
    deps.today = lambda: TODAY
    if config:
        base = ConfigStore.fake().all()
        deps.config = lambda: ConfigStore.from_values({**base, **config})
    return deps


def parsed(gmail, i=0):
    raw = gmail.drafts[i]["raw"]
    return message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def test_dev_routes_every_draft_to_the_dev_inbox(tmp_path):
    gmail = FakeGmail()
    deps = deps_for(settings("dev"), tmp_path, gmail=gmail)
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    assert result.ok and len(gmail.drafts) == 4
    first = parsed(gmail)
    assert first["To"] == "madeshwaranm02@gmail.com"
    assert first["Cc"] is None and first["Bcc"] is None and first["From"] is None
    assert first["Subject"] == ("[DEV] to piotr.example@vistula.example.com | DevOps Engineer on "
                                "your team at Vistula Cloud")
    assert "4 drafts created in Gmail (madeshwaranm02 in DEV)" in result.message


def test_prod_uses_real_recipient_and_plain_subject(tmp_path):
    gmail = FakeGmail()
    deps = deps_for(settings("prod"), tmp_path, gmail=gmail)
    drafter.create_drafts(deps, "fixture-clean-pl")
    first = parsed(gmail)
    assert first["To"] == "piotr.example@vistula.example.com"
    assert first["Subject"] == "DevOps Engineer on your team at Vistula Cloud"


def test_order_templates_and_summary(tmp_path):
    deps = deps_for(settings(), tmp_path)
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    assert [(d.type, d.template) for d in result.drafted] == [
        ("Hiring", "hiring"), ("Recruiter/TA", "recruiter"), ("Peer engineer", "peer"),
        ("Peer engineer", "peer")]
    lines = result.message.splitlines()
    assert lines[:4] == ["Drafts for Vistula Cloud, DevOps Engineer",
                         "Hiring: Piotr Example (hiring template)",
                         "Recruiter/TA: Ola Example (recruiter template)",
                         "Peer engineer: Anna Example, Jan Example (peer template)"]
    assert "Reminder: soft cap is 15 mails a day." in lines


def test_skip_rules(tmp_path):
    result = drafter.create_drafts(deps_for(settings(), tmp_path), "fixture-clean-pl")
    skipped = "; ".join(result.skipped)
    assert "Bob Example (Bounced)" in skipped
    assert "Dora Example (Do not contact)" in skipped
    assert "Olga Example (Type Other)" in skipped
    assert "careers@vistula.example.com (generic mailbox, no Config" in skipped
    assert "Rafal Example (mailed 10 days ago for Site Reliability Engineer" in skipped


def test_generic_mailbox_with_greeting_key(tmp_path):
    deps = deps_for(settings(), tmp_path, config={"mail.generic_greeting_name": "Hiring Team"})
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    generic = [d for d in result.drafted if d.type == "Other"]
    assert [(d.email, d.template) for d in generic] == [("careers@vistula.example.com",
                                                        "recruiter")]
    assert result.mails[-1].body.startswith("Hi Hiring Team,")


def test_dry_run_creates_nothing_and_previews(tmp_path):
    gmail = FakeGmail()
    deps = deps_for(settings(dry="true"), tmp_path, gmail=gmail)
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    assert gmail.drafts == []
    assert deps.contacts.writes == []
    assert deps.jobs.writes == []
    assert "DRY RUN: 4 drafts not created." in result.message
    assert "First mail:" in result.message and "Subject: [DEV] to piotr." in result.message
    assert "Attachment: " in result.message


def test_no_write_flag_is_like_dry_run(tmp_path):
    gmail = FakeGmail()
    deps = deps_for(settings(), tmp_path, gmail=gmail, write=False)
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    assert gmail.drafts == [] and deps.contacts.writes == [] and len(result.mails) == 4


def test_writes_contacts_and_job(tmp_path):
    deps = deps_for(settings(), tmp_path)
    drafter.create_drafts(deps, "fixture-clean-pl")
    row = deps.contacts.rows["ct-hiring"]
    assert row["Gmail draft ID"] == "r-fake-draft-1"
    assert row["Gmail thread ID"] == "fake-thread-1"
    assert row["Status"] == "Drafted"
    assert row["Notes"] == "drafted for DevOps Engineer 2026-09-30"
    assert deps.jobs.get_values("fixture-clean-pl")["Last activity date"] == TODAY
    assert deps.jobs.get_values("fixture-clean-pl")["Status"] == "Resume built"


def test_idempotent(tmp_path):
    gmail = FakeGmail()
    deps = deps_for(settings(), tmp_path, gmail=gmail)
    drafter.create_drafts(deps, "fixture-clean-pl")
    again = drafter.create_drafts(deps, "fixture-clean-pl")
    assert len(gmail.drafts) == 4
    assert again.drafted == [] and "Piotr Example (already drafted)" in again.skipped


def test_hiring_without_detail_gets_recruiter_and_remote_city(tmp_path):
    gmail = FakeGmail()
    deps = deps_for(settings("prod"), tmp_path, gmail=gmail)
    result = drafter.create_drafts(deps, "fixture-no-detail-ie")
    assert [(d.type, d.template) for d in result.drafted] == [("Hiring", "recruiter")]
    assert "Hiring: Niamh Example (recruiter template, no specific detail)" in result.message
    assert parsed(gmail)["Subject"] == "Platform Engineer application, Northwind Cloud Ireland"
    assert "an Irish employment permit" in result.mails[0].body


def test_long_dash_in_template_aborts_that_draft(tmp_path):
    templates = fake_templates()
    bad = dataclasses.replace(templates["hiring"],
                              body=templates["hiring"].body + f" Thanks {LONG_DASH} Alex")
    templates["hiring"] = bad
    deps = deps_for(settings(), tmp_path)
    deps.templates = lambda: templates
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    assert [d.type for d in result.drafted] == ["Recruiter/TA", "Peer engineer", "Peer engineer"]
    assert any("Piotr Example (aborted:" in s and "dash" in s for s in result.skipped)


def test_attachment_is_the_approved_revision(tmp_path):
    deps = deps_for(settings(), tmp_path)
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    name, data = result.mails[0].attachment
    assert name == "Alex_Devops_VistulaCloud.pdf"
    assert data == drafter.fake_pdf("fixture-clean-pl r2")
    assert len(data) <= 250 * 1024


def test_attachment_from_out_in_dry_run(tmp_path):
    deps = deps_for(settings(dry="true"), tmp_path)
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    assert result.mails[0].attachment[1] == drafter.fake_pdf("fixture-clean-pl r2")
    assert (tmp_path / "out" / "fake" / "mail").is_dir()


def test_oversized_attachment_aborts(tmp_path):
    deps = deps_for(settings(), tmp_path, config={"mail.max_attachment_kb": "0"})
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    assert not result.ok and "over the 0 KB limit" in result.message


def test_no_approved_resume(tmp_path):
    deps = deps_for(settings(), tmp_path, resume_log=FakeResumeLogRepo())
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    assert not result.ok and result.message.startswith("No approved resume for this job")


def test_token_without_modify_stops_before_any_draft(tmp_path, monkeypatch):
    monkeypatch.setattr("googleapiclient.discovery.build",
                        lambda *a, **k: pytest.fail("no API client"))
    s = settings(GMAIL_SENDER_TOKEN_JSON='{"scopes": ["https://www.googleapis.com/auth/'
                                        'gmail.readonly"]}')
    deps = deps_for(s, tmp_path)
    deps.gmail = lambda: GmailClient.from_settings(s)
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    assert not result.ok and result.message == NO_MODIFY
    assert deps.contacts.writes == []


def test_unsent_drafts_warning(tmp_path):
    deps = deps_for(settings(), tmp_path, config={"mail.daily_send_cap": "3"})
    result = drafter.create_drafts(deps, "fixture-clean-pl")
    assert "You have 5 unsent drafts; send at most 3 today." in result.message
