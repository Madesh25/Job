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
    # A starter question's answer goes under all its names (Phase 23 4-01).
    answers.command(state, "Driving licence? = No")
    assert answers.listing(state) == ("Your saved answers (3), shown in every Apply pack:\n"
                                      "1. years of terraform = 4 years in production\n"
                                      "2. Driving licence = No\n"
                                      "3. Driver license = No")
    assert answers.command(state, "delete 1") == "Deleted: years of terraform"
    assert answers.command(state, "delete 5").startswith("There is no answer 5.")
    assert answers.pack_lines(state) == ["", "Your saved answers (/answers)",
                                         "- Driving licence No", "- Driver license No"]


def test_empty_pack_part_says_how_to_add():
    assert "/answer <question> = <answer>" in answers.pack_lines(FakeBotState())[-1]


def test_commands_in_the_bot_and_the_apply_pack(desk):  # noqa: F811
    fake = talk(desk, "/answer Earliest interview date? = Any weekday")
    assert fake.sent[0][1].startswith("[LOCAL] Saved: Earliest interview date? = Any weekday")
    assert "1. Earliest interview date? = Any weekday" in talk(desk, "/answers").sent[0][1]
    pack = talk(desk, "/applypack pl-clean").sent[0][1]
    assert "Your saved answers (/answers)\n- Earliest interview date? Any weekday" in pack


def test_many_lines_in_one_message():
    """13 Nov: the starter questions answered in one paste, one answer per line."""
    state = FakeBotState()
    said = answers.command(state, "Years of experience with Kubernetes = 3\n\n"
                                  "Your current employer? = Xerago\n"
                                  "no equals sign here\n"
                                  "Nationality = Indian")
    assert said.startswith("3 answers saved:\nSaved: Years of experience with Kubernetes = 3")
    assert "Not saved (each line needs question = answer):\nno equals sign here" in said
    rows = {(r["q"], r["a"]) for r in answers.items(state)}
    assert ("Years of experience with Kubernetes", "3") in rows
    assert ("Nationality", "Indian") in rows and ("Citizenship", "Indian") in rows
    assert ("Your current employer?", "Xerago") in rows
    # One line is the old /answer.
    assert answers.command(state, "Nationality = Indian").startswith("Replaced: Nationality")
