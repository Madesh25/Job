from jobengine.track.classify import strip_quoted


def test_strip_quoted_history():
    text = ("Thanks, let's talk on Tuesday.\n\nOn Mon, 5 Oct 2026 at 10:00, Alex Example "
            "<alex@example.com> wrote:\n> Hi Ola,\n> I applied...")
    assert strip_quoted(text) == "Thanks, let's talk on Tuesday."
    assert strip_quoted("Hi\n> quoted") == "Hi"
    assert strip_quoted("Sure.\n-----Original Message-----\nFrom: x") == "Sure."
    assert len(strip_quoted("x" * 5000)) == 3000
