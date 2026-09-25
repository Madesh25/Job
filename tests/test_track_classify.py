import base64

from jobengine.gmail_client import FakeGmail, matches_query, to_message
from jobengine.llm import FakeLLM
from jobengine.track.classify import classify, parse, strip_quoted
from jobengine.track.fakes import load_threads
from jobengine.track.models import AUTO_REPLY, INTERVIEW, OPT_OUT


def test_strip_quoted_history():
    text = ("Thanks, let's talk on Tuesday.\n\nOn Mon, 5 Oct 2026 at 10:00, Alex Example "
            "<alex@example.com> wrote:\n> Hi Ola,\n> I applied...")
    assert strip_quoted(text) == "Thanks, let's talk on Tuesday."
    assert strip_quoted("Hi\n> quoted") == "Hi"
    assert strip_quoted("Sure.\n-----Original Message-----\nFrom: x") == "Sure."
    assert len(strip_quoted("x" * 5000)) == 3000


THREADS = load_threads()


def last(thread_id):
    return THREADS[thread_id][-1]


def test_auto_reply_by_header_needs_no_llm(tmp_path):
    llm = FakeLLM(base=tmp_path)
    assert classify(last("t-ola"), llm).kind == AUTO_REPLY
    assert llm.calls == []


def test_opt_out_phrase_without_llm_call(tmp_path):
    llm = FakeLLM(base=tmp_path)
    result = classify(last("t-rita"), llm)
    assert (result.kind, result.confidence, result.source) == (OPT_OUT, 1.0, "phrase")
    assert llm.calls == []


def test_llm_class_and_what_it_sees():
    llm = FakeLLM()
    result = classify(last("t-piotr"), llm)
    assert (result.kind, result.confidence) == (INTERVIEW, 0.93)
    user = llm.calls[0]["user"]
    assert llm.calls[0]["stage"] == "score"
    assert "Sender domain: vistula.example.com" in user
    assert "wrote:" not in user and "invented cold mail text" not in user  # quote stripped
    assert "piotr.example@" not in user  # no address, only the domain


def test_quote_must_be_in_text():
    assert parse({"class": "offer", "confidence": 0.99, "quote": "we are delighted"},
                 "Thanks, no news yet.").confidence == 0.0
    assert parse({"class": "nonsense", "confidence": 2, "quote": "no news"},
                 "Thanks, no news yet.").kind == "other"
    ok = parse({"class": "neutral", "confidence": 2, "quote": "No  news"}, "Thanks, no news.")
    assert ok.confidence == 1.0


def test_llm_error_gives_zero_confidence(tmp_path):
    result = classify(last("t-marek"), FakeLLM(base=tmp_path))  # no fixture: LLMError
    assert result.confidence == 0.0


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_to_message_parses_the_gmail_payload():
    raw = {"id": "m1", "threadId": "t1", "internalDate": "1791619200000", "snippet": "Hi",
           "labelIds": ["INBOX"], "payload": {
               "mimeType": "multipart/alternative",
               "headers": [{"name": "From", "value": "Ola Example <Ola.Example@example.com>"},
                           {"name": "Subject", "value": "Re: hello"},
                           {"name": "Message-ID", "value": "<abc@example.com>"}],
               "parts": [{"mimeType": "text/plain", "body": {"data": _b64("Hello there")}},
                         {"mimeType": "text/html", "body": {"data": _b64("<p>Hello</p>")}}]}}
    m = to_message(raw)
    assert (m.sender, m.subject, m.text, m.message_id) == (
        "ola.example@example.com", "Re: hello", "Hello there", "<abc@example.com>")
    assert m.when.year == 2026 and not m.sent


def test_fake_search_query():
    gmail = FakeGmail(threads=THREADS)
    m = last("t-bounce")
    assert matches_query(m, "after:0 from:(mailer-daemon OR postmaster)")
    assert not matches_query(m, f"after:{int(m.when.timestamp()) + 1}")
    assert not matches_query(last("t-piotr"), "from:(mailer-daemon OR postmaster)")
    found = gmail.search("from:(mailer-daemon OR postmaster) -in:chats")
    assert [x.id for x in found] == ["m-bounce-1", "m-bounce-2"]
