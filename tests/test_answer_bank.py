"""Flow feature 10 (9 Oct): answer a form question once, it is in every later Apply pack."""

from test_telegram_bot import CHAT_ID, desk, talk, update  # noqa: F401  (desk is a fixture)

from jobengine.apply import answers
from jobengine.bot_state import FakeBotState


def test_save_replace_list_and_delete():
    state = FakeBotState()
    assert answers.command(state, "no equals sign") == answers.USAGE
    assert answers.command(state, "Years of Terraform? = 3 years").startswith(
        "Saved: Years of Terraform? = 3 years")
    assert answers.command(state, "years of terraform = 4 years in production").startswith(
        "Replaced: years of terraform = 4 years in production")
    answers.command(state, "Driving licence? = No")
    assert answers.listing(state) == ("Your saved answers (2), shown in every Apply pack:\n"
                                      "1. years of terraform = 4 years in production\n"
                                      "2. Driving licence? = No")
    assert answers.command(state, "delete 1") == "Deleted: years of terraform"
    assert answers.command(state, "delete 5").startswith("There is no answer 5.")
    assert answers.pack_lines(state) == ["", "Your saved answers (/answers)",
                                         "- Driving licence? No"]


def test_empty_pack_part_says_how_to_add():
    assert "/answer <question> = <answer>" in answers.pack_lines(FakeBotState())[-1]


def test_commands_in_the_bot_and_the_apply_pack(desk):  # noqa: F811
    fake = talk(desk, "/answer Earliest interview date? = Any weekday")
    assert fake.sent[0][1].startswith("[LOCAL] Saved: Earliest interview date? = Any weekday")
    assert "1. Earliest interview date? = Any weekday" in talk(desk, "/answers").sent[0][1]
    pack = talk(desk, "/applypack pl-clean").sent[0][1]
    assert "Your saved answers (/answers)\n- Earliest interview date? Any weekday" in pack
