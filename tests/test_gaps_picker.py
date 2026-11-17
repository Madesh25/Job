"""FR-1 (Phase 9 P9-20): "Add skills" under a resume preview lists the job's missing skills;
you tick several and one rebuild adds them all. Picked skills are saved to Skills Inventory
as Hands-on (your yes of 23 Oct)."""

from test_telegram_bot import ButtonTelegram, desk, settings, talk, tap  # noqa: F401

from jobengine import telegram_bot as tb


def picker(d):
    d.repo.rows["pl-clean"]["Gaps"] = ("Istio, Kafka, Experience designing CI/CD pipelines, "
                                       "Kubernetes")
    d.tap("ap:pl-clean")
    [reply] = d.tap("gp:pl-clean")
    return reply


def test_the_picker_lists_skill_names_that_are_not_yours(desk):  # noqa: F811
    reply = picker(desk)
    assert reply.column  # one button per row
    assert reply.buttons == [("☐ Istio", "gt:pl-clean:0"), ("☐ Kafka", "gt:pl-clean:1"),
                             ("Cancel", "gx:pl-clean")]  # a sentence and Kubernetes left out


def test_ticking_two_and_rebuilding_once_adds_both(desk):  # noqa: F811
    picker(desk)
    desk.tap("gt:pl-clean:0")
    [ticked] = desk.tap("gt:pl-clean:1")
    assert ticked.edit and "Picked: Istio, Kafka" in ticked.text
    assert ("Rebuild with 2 skills", "gr:pl-clean") in ticked.buttons
    [untick] = desk.tap("gt:pl-clean:1")
    assert "Picked: Istio" in untick.text and "Kafka" not in untick.text.split("Picked:")[1]
    desk.tap("gt:pl-clean:1")
    note, *rebuilt = desk.tap("gr:pl-clean")
    assert note.text.startswith("Rebuilding with Istio, Kafka. Added to Skills Inventory as "
                                "Hands-on: Istio, Kafka.")
    assert [(n, lvl) for n, lvl, _ in desk.skills_added] == [("Istio", "Hands-on"),
                                                             ("Kafka", "Hands-on")]
    preview = next(r for r in rebuilt if "(version 2)" in r.text)
    assert "Istio" in preview.text and "Kafka" in preview.text
    assert desk.tap("gr:pl-clean")[0].text.startswith("That list is no longer open")


def test_cancel_adds_nothing(desk):  # noqa: F811
    picker(desk)
    desk.tap("gt:pl-clean:0")
    assert desk.tap("gx:pl-clean")[0].text == "Nothing added."
    assert desk.skills_added == []


def test_a_tick_edits_the_picker_message_in_telegram(desk):  # noqa: F811
    picker(desk)

    class Editable(ButtonTelegram):
        def __call__(self, method, payload, timeout):
            if method == "editMessageText":
                self.calls.append((method, payload))
                return {"ok": True, "result": {}}
            return super().__call__(method, payload, timeout)

    fake = Editable()
    client = tb.TelegramClient(fake)
    query = {"id": "cb1", "from": {"id": int(settings().telegram_chat_id)},
             "message": {"message_id": 77, "chat": {"id": settings().telegram_chat_id}},
             "data": "gt:pl-clean:0"}
    tb.handle_callback(client, settings(), query, desk)
    method, payload = next((m, p) for m, p in fake.calls if m == "editMessageText")
    assert payload["message_id"] == 77
    rows = payload["reply_markup"]["inline_keyboard"]
    assert rows[0] == [{"text": "☑ Istio", "callback_data": "gt:pl-clean:0"}]
    assert not any(m == "sendMessage" for m, _ in fake.calls)
