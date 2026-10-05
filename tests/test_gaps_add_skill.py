"""/gaps buttons: add a skill you have to Skills Inventory (your idea of 5 Oct)."""

from test_review_one_at_a_time import make_desk

from jobengine.notion_repo import skill_writer
from jobengine.settings import load_settings


def desk_with_gaps():
    d = make_desk()
    page = next(iter(d.repo.rows))
    d.repo.rows[page]["Gaps"] = "ArgoCD, Experience designing and maintaining CI/CD pipelines"
    return d


def test_gaps_offers_a_hands_on_and_a_production_button_per_short_skill():
    d = desk_with_gaps()
    replies = d.gaps_command()
    buttons = [r for r in replies if r.buttons]
    asked = {r.text.split(":")[0] for r in buttons}
    assert "ArgoCD" in asked
    assert not any("Experience designing" in r.text for r in buttons)  # a sentence, no button
    argo = next(r for r in buttons if r.text.startswith("ArgoCD"))
    assert argo.buttons == [("Hands-on", "gs:h:ArgoCD"), ("Production", "gs:p:ArgoCD")]


def test_a_tap_adds_the_skill_once_at_that_level():
    d = desk_with_gaps()
    [reply] = d.tap("gs:h:ArgoCD")
    assert reply.text.startswith("Added ArgoCD to Skills Inventory as Hands-on.")
    assert d.skills_added[0][:2] == ("ArgoCD", "Hands-on")


def test_a_skill_you_already_have_gets_no_button():
    d = desk_with_gaps()
    page = next(iter(d.repo.rows))
    d.repo.rows[page]["Gaps"] = "Kubernetes, ArgoCD"
    texts = [r.text for r in d.gaps_command() if r.buttons]
    assert not any(t.startswith("Kubernetes") for t in texts)


def test_the_writer_posts_one_row_to_skills_inventory_only():
    calls = []

    class Client:
        def request(self, method, path, body=None):
            calls.append((method, path, body))
            return {"id": "x"}

    s = load_settings("local", {})
    add = skill_writer(Client(), s)
    add("ArgoCD", "Production", "Added from /gaps on 2026-10-05.")
    [(method, path, body)] = calls
    assert (method, path) == ("POST", "/pages")
    assert body["parent"]["data_source_id"] == s.notion_read["skills_inventory"]
    assert body["properties"]["Level"] == {"select": {"name": "Production"}}
