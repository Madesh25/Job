"""Flow feature 12 (9 Oct): LinkedIn notes per contact, copied and sent by you."""

from test_telegram_bot import desk, talk, tap  # noqa: F401  (desk is a fixture)

from jobengine.contacts import linkedin_notes as ln

CONTACTS = [{"Name": "Kasia Example", "Type": "Peer engineer"},
            {"Name": "Ola Recruiter", "Type": "Recruiter/TA"},
            {"Name": "info@x.example", "Type": "Other"}]


def test_notes_per_type_within_300_characters():
    text = ln.notes(CONTACTS, role="DevOps Engineer", company="Vistula Cloud",
                    link="https://jobs.example.com/pl-clean", me="Madeshwaran", templates={})
    assert text.startswith("LinkedIn notes for Vistula Cloud, DevOps Engineer")
    assert 'Kasia Example (Peer engineer), search "Kasia Example Vistula Cloud" on LinkedIn:\n'\
        "Hi Kasia, I applied for the DevOps Engineer role at Vistula Cloud" in text
    assert "Would you be open to referring me" in text and "Hi Ola," in text
    assert "info@x.example" not in text  # no note for a mailbox


def test_long_link_is_dropped_then_cut_at_a_word():
    note = ln.note(ln.DEFAULTS["peer"], name="Kasia", role="Senior Platform Engineer",
                   company="Vistula Cloud", link="https://x.example/" + "a" * 250,
                   me="Madeshwaran")
    assert "on your careers page" in note and len(note) <= ln.NOTE_LIMIT
    cut = ln.note("{first} " + "word " * 100, name="K", role="", company="", link="", me="")
    assert len(cut) <= ln.NOTE_LIMIT and not cut.endswith(" ")


def test_your_own_wording_and_no_contacts():
    text = ln.notes(CONTACTS[:1], role="R", company="C", link="", me="M",
                    templates={"peer": "Hello {first} from {me}"})
    assert "Hello Kasia from M" in text
    assert ln.notes([], role="R", company="C", link="", me="M", templates={}).startswith(
        "No engineer, recruiter or hiring manager")


def test_button_under_the_contacts(desk):  # noqa: F811
    talk(desk, "/contacts https://jobs.example.com/pl-clean")
    fake = talk(desk, tap(2, "ln:pl-clean"))
    assert fake.sent[-1][1].startswith("[LOCAL] LinkedIn notes for Vistula Cloud, DevOps Engineer")
    assert "Hi Kasia," in fake.sent[-1][1]
