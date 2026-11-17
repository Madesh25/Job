"""Notion counts 2000 characters per text item in UTF-16: an emoji counts as 2."""

from jobengine import notion_repo
from jobengine.notion_repo import clip, paragraph_blocks, rich_text, text_pieces


def utf16(text):
    return len(text.encode("utf-16-le")) // 2


def test_pieces_count_emoji_as_two():
    text = "a" * 1999 + "\U0001F680" + "b"  # 2001 characters by Python, 2002 for Notion
    pieces = text_pieces(text)
    assert pieces == ["a" * 1999, "\U0001F680b"]
    assert all(utf16(p) <= 2000 for p in pieces)
    assert "".join(pieces) == text


def test_every_writer_stays_under_the_limit():
    text = ("\U0001F680 DevOps " * 400)[:2000]  # 2000 by Python, more for Notion
    for item in paragraph_blocks([text])[0]["paragraph"]["rich_text"] + rich_text(text):
        assert utf16(item["text"]["content"]) <= 2000
    title = notion_repo.notion_value("title", text)["title"][0]["text"]["content"]
    assert utf16(title) <= 2000 and utf16(clip(text)) <= 2000


def test_plain_text_is_unchanged():
    assert text_pieces("") == [] and clip("") == ""
    assert text_pieces("x" * 4000) == ["x" * 2000, "x" * 2000]
    assert paragraph_blocks(["short"])[0]["paragraph"]["rich_text"] == [
        {"type": "text", "text": {"content": "short"}}]


def test_one_refused_job_does_not_stop_the_sweep():
    from test_runner import TODAY, S

    from jobengine import http
    from jobengine.sweep.runner import fake_deps, run_sweep

    deps = fake_deps(S)
    real_create = deps.repo.create
    calls = []

    def create(plan, blocks):
        calls.append(plan)
        if len(calls) == 1:
            raise http.HttpError("POST https://api.notion.com/v1/pages failed: HTTP 400", 400)
        return real_create(plan, blocks)

    deps.repo.create = create
    summary = run_sweep(S, deps, TODAY)
    assert len(calls) > 1 and summary.new == len(calls) - 1
    assert len(summary.not_written) == 1
    assert "Could not be saved to Notion (the rest were saved; details in the log): 1" in (
        summary.friendly_text())
