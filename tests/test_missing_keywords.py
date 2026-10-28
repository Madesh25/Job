"""#13 (28 Oct): under a resume preview, "Add N keywords" rebuilds the resume with the job's
keywords you have (Skills Inventory) that the resume left out. Never a skill you lack."""

from test_telegram_bot import desk  # noqa: F401

from jobengine.screen import desk as desk_mod


def preview(d, monkeypatch, left):
    monkeypatch.setattr(type(d), "resume_match",
                        lambda self, outcome: ("Resume match: shows 5 of 8 job keywords you "
                                               "have (not on it: ...; tap Add keywords).", left))
    replies = d.tap("ap:pl-clean")
    return next(r for r in replies if r.document)


def test_the_button_rebuilds_with_the_keywords_you_have(desk, monkeypatch):  # noqa: F811
    shown = preview(desk, monkeypatch, ["Kubernetes", "Terraform", "Istio"])
    assert ("Add 3 keywords", "mk:pl-clean") in shown.buttons
    assert "tap Add keywords" in shown.text
    monkeypatch.setattr(type(desk), "resume_match", lambda self, outcome: (None, []))
    note, *rebuilt = desk.tap("mk:pl-clean")
    # Istio is not in your Skills Inventory: it is never added.
    assert note.text == "Rebuilding with Kubernetes, Terraform (all in your Skills Inventory)."
    assert any("(version 2)" in r.text for r in rebuilt)
    assert desk.tap("mk:pl-clean")[0].text.startswith("That list is no longer open")


def test_no_button_when_the_resume_shows_them_all(desk, monkeypatch):  # noqa: F811
    shown = preview(desk, monkeypatch, [])
    assert not any(data.startswith("mk:") for _, data in shown.buttons)


def test_at_most_eight_keywords(desk, monkeypatch):  # noqa: F811
    many = [f"Tool{i}" for i in range(12)]
    shown = preview(desk, monkeypatch, many)
    assert (f"Add {desk_mod.MAX_MISSING_TERMS} keywords", "mk:pl-clean") in shown.buttons
    assert desk.state.get(desk_mod.MISSING_KEY)["terms"] == many[:desk_mod.MAX_MISSING_TERMS]
