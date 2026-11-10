"""/jd (and /linkedin): jobs without a description one at a time, every board (1 Oct idea;
10 Nov: one flow for LinkedIn, JustJoin IT, Pracuj.pl, IrishJobs and the rest)."""

from test_review_one_at_a_time import make_desk

from jobengine.screen.desk import LINKEDIN_PASTE


def test_the_queue_shows_a_job_and_opens_the_paste():
    d = make_desk()
    [reply] = d.jd("")
    assert reply.text.startswith("Job 1 of 2 without a description (LinkedIn):")
    assert LINKEDIN_PASTE in reply.text and "Firefox" in reply.text
    assert [data for _, data in reply.buttons] == ["li:done", "li:next:1", "li:skip:li-no-jd"]
    assert d.captures.get().page_id == "li-no-jd"
    assert d.linkedin_command()[0].text == reply.text  # /linkedin is the same flow
    [other] = d.tap("li:next:1")
    assert other.text.startswith("Job 2 of 2 without a description (") and "LinkedIn" not in (
        other.text.splitlines()[0])


def test_paste_then_done_saves_screens_and_shows_the_next_job():
    d = make_desk()
    d.tap("li:next:0")
    d.text("We are hiring a DevOps Engineer to run Kubernetes on AWS with Terraform. " * 20)
    replies = d.tap("li:done")
    assert replies[0].text.startswith("Saved the description (")
    assert d.repo.rows["li-no-jd"]["Screen verdict"] != "Unscreened"
    assert replies[-1].text == "1 more job(s) wait for a description."
    assert replies[-1].buttons == [("Next job", "li:next:0")]


def test_done_without_a_paste_shows_the_next_job_not_an_error():
    """3-06 (10 Nov): Done after the saved page said "Nothing to finish"."""
    d = make_desk()
    d.tap("li:next:0")
    d.captures.clear()  # the saved page was taken
    replies = d.tap("li:done")
    assert replies[0].text.startswith("Job 1 of ")
    assert "Nothing to finish" not in replies[0].text


def test_not_interested_declines_it():
    d = make_desk()
    d.jd("")
    replies = d.tap("li:skip:li-no-jd")
    assert replies[0].text == "Skipped that job."
    assert d.repo.rows["li-no-jd"]["Status"] == "Declined"
    assert replies[-1].text.startswith("Job 1 of 1 without a description (")


def test_jd_list_still_lists_them():
    d = make_desk()
    [reply] = d.jd("list")
    assert "wait for a description" in reply.text
