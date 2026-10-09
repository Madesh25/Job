"""Flow feature 9 (9 Oct): the form autofill code and the bookmark that fills the form."""

import hashlib
import shutil
import subprocess
from urllib.parse import unquote

import pytest
from test_telegram_bot import desk, settings, talk  # noqa: F401  (desk is a fixture)

from jobengine.apply import answers, autofill
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigRow, ConfigStore

S = settings()
JOB = {"Company": "Acme", "Role": "DevOps Engineer", "Country": "Netherlands"}


def test_fields_are_your_answers_and_nothing_guessed():
    f = autofill.fields(S, ConfigStore.fake(), JOB, letter="Dear Hiring Team,", why="Because.")
    assert f["full_name"] == "Alex Example"
    assert (f["first_name"], f["last_name"]) == ("Alex", "Example")
    assert f["email"] == "alex@example.com" and f["phone"] == "+1 555 010 0000"
    assert f["sponsorship_needed"] == "Yes" and f["authorized_without_sponsorship"] == "No"
    assert f["city"] == "Chennai, India"
    assert (f["cover_letter"], f["why"]) == ("Dear Hiring Team,", "Because.")
    rows = {k: v for k, v in ConfigStore.fake()._rows.items()
            if k not in ("profile.name", "profile.phone")}
    s = S.model_copy(update={"apply_pack": {k: v for k, v in S.apply_pack.items()
                                            if k != "notice_period"}})
    f = autofill.fields(s, ConfigStore(rows), JOB)
    assert not {"full_name", "first_name", "last_name", "phone", "notice_period",
                "cover_letter", "why"} & set(f)


def test_config_names_win():
    config = ConfigStore(dict(ConfigStore.fake()._rows) | {
        "apply.last_name": ConfigRow(key="apply.last_name", value="Example-Smith")})
    assert autofill.fields(S, config, JOB)["last_name"] == "Example-Smith"


def test_code_round_trip_with_saved_answers():
    state = FakeBotState()
    answers.command(state, "Years of Terraform? = 3 years in production")
    text = autofill.message(S, ConfigStore.fake(), state, JOB, letter="Letter.")
    assert text.startswith(autofill.HOW) and "/autofill" in text
    data = autofill.decode(text)
    assert data["job"] == "Acme, DevOps Engineer"
    assert data["answers"] == [["Years of Terraform?", "3 years in production"]]
    assert data["fields"]["cover_letter"] == "Letter."


def test_long_code_drops_old_answers_then_the_letter():
    state = FakeBotState()
    for i in range(60):  # hashes: text that does not compress
        noise = hashlib.sha256(str(i).encode()).hexdigest()
        answers.command(state, f"Question {i} {noise[:30]}? = answer {i} {noise[30:]}")
    letter = " ".join(f"word{i}" for i in range(400))
    code = autofill.code(S, ConfigStore.fake(), state, JOB, letter=letter)
    assert len(code) <= autofill.MAX_CODE
    data = autofill.decode(code)
    assert data["answers"][-1][1].startswith("answer 59")  # the newest are kept
    assert len(data["answers"]) < 60
    big = "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(6000))  # not compressible
    data = autofill.decode(autofill.code(S, ConfigStore.fake(), FakeBotState(), JOB, letter=big))
    assert "cover_letter" not in data["fields"] and data["fields"]["email"]


def test_bookmarklet_never_submits():
    js = autofill.AUTOFILL_JS
    assert ".submit(" not in js and "requestSubmit" not in js
    assert "fetch(" not in js and "XMLHttpRequest" not in js  # nothing leaves the page
    assert "//" not in js.replace("://", "")  # joined into one line: no line comments
    mark = autofill.bookmarklet()
    assert mark.startswith("javascript:(async") and " " not in mark and "#" not in mark
    assert "JE1:" in unquote(mark)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_bookmarklet_is_valid_javascript(tmp_path):
    path = tmp_path / "bookmark.js"
    path.write_text(unquote(autofill.bookmarklet()[len("javascript:"):]), encoding="utf-8")
    subprocess.run(["node", "--check", str(path)], check=True, capture_output=True)


def test_autofill_in_the_bot_and_the_apply_pack(desk):  # noqa: F811
    sent = [text for _chat, text in talk(desk, "/autofill").sent]
    assert "set up once per browser" in sent[0]
    assert any("javascript:" in text for text in sent[1:])
    sent = [text for _chat, text in talk(desk, "/applypack pl-clean").sent]
    code = next(text for text in sent if autofill.CODE_PREFIX in text)
    assert code.startswith("[LOCAL] " + autofill.HOW) or autofill.HOW in code
    assert autofill.decode(code)["fields"]["email"]
