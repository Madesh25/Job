"""/fetchcontacts: contacts and Gmail drafts for every job applied today, after the review."""

from test_review_one_at_a_time import approve_resume, make_desk

from jobengine import telegram_bot as tb


def applied(d, job):
    if job != "pl-clean":
        d.tap(f"ap:{job}")
    rows = d.resume.resume_log.all_rows()
    log_id = [pid for pid, v in rows if job in (v.get("Job") or [])][-1]
    d.tap("ra:" + log_id.replace("-", ""))
    d.tap(f"ia:{job}")


def two_applied():
    d = make_desk()
    approve_resume(d)
    d.tap("ia:pl-clean")
    applied(d, "nl-sponsor-yes")
    return d


def test_nothing_applied_today():
    d = make_desk()
    assert d.fetch_contacts_command()[0].text == (
        "No job is marked I applied today (2026-10-01). Tap I applied under a job first.")


def test_the_list_comes_first_and_nothing_is_looked_up():
    d = two_applied()
    ask = d.fetch_contacts_command()[0]
    assert ask.text.startswith("Applied today (2026-10-01): 2 job(s).\n"
                               "- Vistula Cloud, DevOps Engineer: needs a lookup\n"
                               "- Tulip Data B.V., Cloud Engineer: needs a lookup\n")
    assert "Paid calls are off here" in ask.text
    assert "Gmail drafts with the resume attached (never sent by themselves)" in ask.text
    assert ask.buttons == [("Go", "fx:2026-10-01")]
    assert not d.repo.rows["pl-clean"].get("Contacts") and not d.contacts.calls


def test_go_finds_contacts_then_writes_drafts_for_each_job():
    d = two_applied()
    replies = d.tap("fx:2026-10-01")
    texts = [r.text for r in replies]
    assert texts[0] == "1/2 Vistula Cloud, DevOps Engineer..."
    assert texts[1].startswith("Drafts for Vistula Cloud, DevOps Engineer")
    assert "DRY RUN: 4 drafts not created." in texts[1]
    assert d.repo.rows["pl-clean"]["Contacts"]
    assert texts[2] == "2/2 Tulip Data B.V., Cloud Engineer..."
    assert texts[3].startswith("What is the email domain for Tulip Data B.V.?")
    assert texts[-1] == (
        "Done: 2 job(s) applied today, 4 Gmail draft(s). DRY RUN: nothing was created in "
        "Gmail.\nWaiting for the email domain (answer the question, then tap Write Gmail "
        "drafts): Tulip Data B.V., Cloud Engineer")


def test_saved_contacts_are_used_without_a_new_lookup():
    d = two_applied()
    d.tap("fx:2026-10-01")
    ask = d.fetch_contacts_command()[0].text
    assert "- Vistula Cloud, DevOps Engineer: 4 saved contact(s)" in ask
    calls = list(d.contacts.calls)
    replies = d.tap("fx:2026-10-01")
    assert replies[1].text.startswith("Drafts for Vistula Cloud, DevOps Engineer")
    # Only Tulip (still no domain) went to the lookup again.
    assert d.contacts.calls == calls


def test_an_old_go_button_does_nothing():
    d = two_applied()
    assert d.tap("fx:2026-09-30")[0].text == (
        "That list was for another day. Send /fetchcontacts again.")
    assert d.tap("fx:junk")[0].text == "That button is no longer valid. Send /fetchcontacts."
    assert not d.contacts.calls


def test_the_command_is_in_the_menu():
    assert "/fetchcontacts" in tb.HELP_TEXT
    assert "fetchcontacts" in {c["command"] for c in tb.bot_commands()}
