"""/linkedin: LinkedIn jobs one at a time, paste the description, Done (1 Oct idea)."""

from test_review_one_at_a_time import make_desk

from jobengine.screen.desk import LINKEDIN_PASTE


def test_linkedin_queue_shows_a_job_and_opens_the_paste():
    d = make_desk()
    [reply] = d.linkedin_command()
    assert reply.text.startswith("LinkedIn job 1 of 1:")
    assert LINKEDIN_PASTE in reply.text
    assert [data for _, data in reply.buttons] == ["li:done", "li:next:1", "li:skip:li-no-jd"]
    assert d.captures.get().page_id == "li-no-jd"


def test_paste_then_done_saves_screens_and_shows_the_card():
    d = make_desk()
    d.tap("li:next:0")
    d.text("We are hiring a DevOps Engineer to run Kubernetes on AWS with Terraform. " * 20)
    replies = d.tap("li:done")
    assert replies[0].text.startswith("Saved the description (")
    assert d.repo.rows["li-no-jd"]["Screen verdict"] != "Unscreened"
    assert d.linkedin_rows() == []


def test_not_interested_declines_it():
    d = make_desk()
    d.linkedin_command()
    replies = d.tap("li:skip:li-no-jd")
    assert replies[0].text == "Skipped that LinkedIn job."
    assert d.repo.rows["li-no-jd"]["Status"] == "Declined"
    assert replies[-1].text.startswith("No LinkedIn jobs wait")
