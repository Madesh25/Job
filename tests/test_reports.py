"""Report a problem from Telegram (S2, 23 Nov, your ask): "wrong: ..." as a reply, or /bug,
is one Error Log row with the bot message attached. Made-up messages, no Notion."""

from datetime import datetime, timedelta, timezone

from test_telegram_bot import CHAT_ID, Clock, settings

from jobengine import telegram_bot as tb
from jobengine.notion_repo import FakeTable
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import reports

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 23, 21, 50, tzinfo=IST)
BOT_SAID = ("📊 Daily report, Mon 23 Nov\n📊 Today: Found 0 · Screened 4\n"
            "mail to jane.doe@example.com")


def message(text, replied=None):
    msg = {"message_id": 77, "chat": {"id": int(CHAT_ID)}, "text": text}
    if replied is not None:
        msg["reply_to_message"] = replied
    return {"update_id": 1, "message": msg}


def bot_message(text=BOT_SAID):
    return {"message_id": 70, "date": int(NOW.timestamp()) - 300, "text": text}


def test_the_words_of_a_report():
    assert reports.words("wrong: Found 0 but the fetch saved 3") == \
        "Found 0 but the fetch saved 3"
    assert reports.words("Wrong - the count") == "the count"
    assert reports.words("bug: x") == "x"
    assert reports.words("/bug the menu is gone") == "the menu is gone"
    assert reports.words("/bug@MadeshDevBot two lines\nsecond") == "two lines\nsecond"
    assert reports.words("/bug") == ""
    assert reports.words("3 = the right answer") is None
    assert reports.words("something is wrong here") is None  # no "wrong:" at the start
    assert reports.words("/bugs") is None


def test_the_row_has_your_words_and_the_message_blanked():
    row = reports.row("Found 0 is wrong", bot_message(), NOW)
    assert row["Problem"] == "You: Found 0 is wrong"
    assert row["Level"] == "Reported by you" and row["Status"] == "New"
    assert row["Likely cause"] == "Your report" and row["Count"] == 1
    assert row["Key"] == "2026-11-23|report-70"
    assert row["Example"].startswith("Your words: Found 0 is wrong\n\nThe bot's message "
                                     "(23 Nov 21:45):\n📊 Daily report")
    assert "jane.doe@example.com" not in row["Example"]
    alone = reports.row("the menu is gone", None, NOW)
    assert "No bot message attached" in alone["Example"]


def test_a_wrong_reply_is_saved_with_the_message(tmp_path):
    desk = fake_desk(settings(), FAKE_TODAY, now=Clock())
    desk.error_log_table = FakeTable()
    desk._track_now = lambda: NOW
    replies = tb.desk_replies(message("wrong: Found 0 but 3 new jobs", bot_message()),
                              settings(), desk)
    assert replies[0].text.startswith("📝 Saved in the Error Log, Status New, with the "
                                      "message you replied to")
    [row] = desk.error_log_table.created
    assert row["Problem"] == "You: Found 0 but 3 new jobs"
    assert "Today: Found 0" in row["Example"]


def test_bug_without_words_says_how_and_saves_nothing():
    desk = fake_desk(settings(), FAKE_TODAY, now=Clock())
    desk.error_log_table = FakeTable()
    replies = tb.desk_replies(message("/bug"), settings(), desk)
    assert replies[0].text == reports.HOW
    assert desk.error_log_table.created == []


def test_no_error_log_or_notion_down_is_told():
    assert reports.save(None, lambda: NOW, "x", None).startswith("Not saved: the Error Log")

    class Down(FakeTable):
        def create(self, *a, **k):
            raise RuntimeError("HTTP 502")

    assert reports.save(Down(), lambda: NOW, "x", None).startswith("Not saved: Notion")
    assert tb.reply_for("/bug the menu", settings()).startswith("Not saved: a report needs")


def test_bug_is_in_help_and_the_menu():
    assert "/bug <what> - report a problem\n" in tb.HELP_TEXT
    assert any(c["command"] == "bug" for c in tb.bot_commands())
