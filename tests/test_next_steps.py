"""What to do next (9 Oct): a "Next:" line after each answer, and /next for the whole day."""

from test_review_one_at_a_time import make_desk

from jobengine import next_steps, telegram_bot
from jobengine.screen.desk import Reply


def test_screen_points_to_pending_and_jd():
    text = ("Screening done: 9 screened (...), 56 waiting for JD.\n"
            "55 ready to review: /pending")
    line = next_steps.hint("screen", "", text)
    assert line.startswith("/pending to review") and "/jd to paste" in line


def test_batch_points_to_collect():
    line = next_steps.hint("screen", "batch", "Sent 12 jobs to the half-price batch.")
    assert line.startswith("/screen collect")


def test_no_hint_on_a_usage_line_or_an_answer_with_its_own_next():
    assert next_steps.hint("drafts", "", "Send /drafts <job URL or page id>.") is None
    assert next_steps.hint("screen", "", "x\n\nNext: tap Write Gmail drafts") is None
    assert next_steps.hint("pending", "", "card") is None


def test_hint_goes_on_the_last_text_or_its_own_message_after_buttons():
    out = telegram_bot.with_next_step("/rules", [Reply("Rules: ...")])
    assert out[-1].text == "Rules: ...\n\n\U0001F449 Next: /fetch for today's jobs."
    card = Reply("Apply high", [("Approve", "ap:1")])
    out = telegram_bot.with_next_step("/screen", [Reply("55 ready to review: /pending"), card])
    assert out[1] is card and out[2].text.startswith("\U0001F449 Next: /pending")


def test_checklist_marks_done_and_the_step_to_do_now():
    state = next_steps.DayState(fetched_today=True, unscreened=0, waiting_jd=3, pending=5,
                                applied_today=0, contacts_done=False)
    text = next_steps.checklist(state)
    assert "✅ /fetch" in text and "✅ /screen" in text
    assert "\U0001F449 /jd: paste descriptions the bot cannot read (3 waiting)" in text
    assert "⏳ /pending: review and Approve (5 ready)" in text
    assert text.endswith("Do now: /jd")


def test_next_command_on_the_desk():
    [reply] = make_desk().next_command()
    assert reply.text.startswith("Your day, in order:")
    assert "Do now: /fetch" in reply.text  # no /fetch today in the fake world


def test_help_is_grouped_like_the_command_guide_and_fits_one_message():
    """H (27 Oct): the five groups of the Command guide, in the order of the day."""
    text = telegram_bot.HELP_TEXT
    heads = [line for line in text.splitlines() if line and not line.startswith(("/", "Job"))]
    assert [h.split(" ", 1)[1] for h in heads] == [
        "Find and screen", "Review and apply", "Contacts and mails", "Replies and interviews",
        "Strategy, health and control"]
    assert text.index("/fetch -") < text.index("/pending -") < text.index("/fetchcontacts -") \
        < text.index("/today -") < text.index("/update -")
    assert len("[LOCAL] Job Engine bot is running (env=local).\n\n" + text) <= \
        telegram_bot.MESSAGE_LIMIT
    names = [c["command"] for c in telegram_bot.bot_commands()]
    assert len(names) == len(set(names))


def test_commands_that_ended_without_a_next_step_get_one():
    assert next_steps.hint("credits", "", "Hunter: 20 left").startswith("/outreach")
    assert next_steps.hint("stats", "", "Applied: 12").startswith("/gaps")
    assert next_steps.hint("keywords", "", "LinkedIn keywords: ...").startswith("put the words")
    assert next_steps.hint("connections", "", "LinkedIn connections: 300").startswith("/pending")
    assert next_steps.hint("agencies", "", "Poland:\n- Hays Poland") == (
        "/agencies <name> for an agency's intro mail.")
    assert next_steps.hint("agencies", "hays", "Which agency? Hays Poland, Hays Ireland") is None
    assert next_steps.hint("mailmode", "send", "Mail mode: send.").startswith("/autopilot")
    assert next_steps.hint("mailmode", "later", "Unknown mail mode 'later'.") is None
    assert next_steps.hint("prep", "x", "... /thanks <job link> gives the mail") is None


def test_next_adds_the_weekly_and_monthly_steps_when_they_are_due():
    from datetime import timedelta

    from jobengine.contacts import connections

    desk = make_desk()
    today = desk.today()
    [reply] = desk.next_command()
    assert "Also due:\n\U0001F4C5 /stats: your reply rates" in reply.text
    assert "\U0001F4C5 /keywords: words for your LinkedIn profile (monthly)" in reply.text
    for command in ("stats", "gaps", "keywords"):
        desk.note_command(command)
    desk.state.set(connections.STAMP_KEY, {"date": (today - timedelta(days=31)).isoformat(),
                                            "count": 300})
    text = desk.next_command()[0].text
    assert "/stats:" not in text and "/keywords:" not in text
    assert "LinkedIn export: send the new Connections zip to the bot" in text
    desk.state.set(next_steps.USED_KEY, {"stats": (today - timedelta(days=7)).isoformat(),
                                         "gaps": today.isoformat(),
                                         "keywords": (today - timedelta(days=29)).isoformat()})
    text = desk.next_command()[0].text
    assert "/stats:" in text and "/gaps:" not in text and "/keywords:" not in text


def test_sending_a_weekly_command_records_it(tmp_path):
    from test_telegram_bot import Clock, fake_rendering, settings, talk

    from jobengine.screen.desk import fake_desk
    from jobengine.sweep.fakes import FAKE_TODAY

    desk = fake_desk(settings(), FAKE_TODAY, now=Clock())
    fake_rendering(desk, tmp_path)
    talk(desk, "/stats")
    assert desk.state.get(next_steps.USED_KEY) == {"stats": desk.today().isoformat()}
