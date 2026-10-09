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
