"""/jd (and /linkedin): jobs without a description one at a time (1 Oct idea; 10 Nov: one
flow for every board, a button per board, only the jobs of the last 3 days)."""

from test_review_one_at_a_time import make_desk

from jobengine.screen.desk import JD_DAYS, LINKEDIN_PASTE


def test_jd_shows_a_button_per_board_with_the_jobs_of_the_last_3_days():
    d = make_desk()
    [menu] = d.jd("")
    assert menu.text.startswith(f"1 job(s) of the last {JD_DAYS} days wait for a description "
                                "(1 older one(s) not shown: /jd list lists them all)")
    assert menu.buttons == [("LinkedIn (1)", "jb:LinkedIn"), ("All (1)", "jb:")]


def test_a_board_shows_its_jobs_one_at_a_time_and_opens_the_paste():
    d = make_desk()
    [reply] = d.tap("jb:LinkedIn")
    assert reply.text.startswith("Job 1 of 1 without a description (LinkedIn, posted ")
    assert LINKEDIN_PASTE in reply.text and "Firefox" in reply.text
    assert [data for _, data in reply.buttons] == ["li:done", "li:next:1", "li:skip:li-no-jd"]
    assert d.captures.get().page_id == "li-no-jd"
    assert d.linkedin_command()[0].text == reply.text  # /linkedin opens the LinkedIn board


def test_paste_then_done_saves_screens_and_shows_its_card():
    d = make_desk()
    d.tap("jb:LinkedIn")
    d.text("We are hiring a DevOps Engineer to run Kubernetes on AWS with Terraform. " * 20)
    replies = d.tap("li:done")
    assert replies[0].text.startswith("Saved the description (")
    assert d.repo.rows["li-no-jd"]["Screen verdict"] != "Unscreened"
    assert replies[-1].text == "No more jobs wait for a description."


def test_done_without_a_paste_shows_the_next_job_not_an_error():
    """3-06 (10 Nov): Done after the saved page said "Nothing to finish"."""
    d = make_desk()
    d.tap("jb:")
    d.captures.clear()  # the saved page was taken
    replies = d.tap("li:done")
    assert replies[0].text.startswith("Job 1 of ")
    assert "Nothing to finish" not in replies[0].text


def test_not_interested_declines_it():
    d = make_desk()
    d.tap("jb:LinkedIn")
    replies = d.tap("li:skip:li-no-jd")
    assert replies[0].text == "Skipped that job."
    assert d.repo.rows["li-no-jd"]["Status"] == "Declined"
    assert replies[-1].text.startswith("No jobs from the last 3 days wait for a description "
                                       "on LinkedIn")


def test_jd_list_still_lists_them_all():
    d = make_desk()
    [reply] = d.jd("list")
    assert "2 jobs wait for a description" in reply.text
