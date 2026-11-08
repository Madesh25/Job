"""/pending one country at a time (your request of 30 Sep: switch the VPN once per country)."""

from test_review_one_at_a_time import is_card, make_desk

from jobengine import telegram_bot as tb
from jobengine.screen.desk import CHOOSE_COUNTRY, country_label


def card_country(desk, reply):
    pid = next(data for _, data in reply.buttons if data.startswith("ap:")).split(":", 1)[1]
    return desk.repo.rows[pid]["Country"]


def test_pending_asks_for_a_country_first():
    d = make_desk()
    [menu] = d.pending_command()
    assert menu.text == CHOOSE_COUNTRY
    assert menu.buttons == [("Poland (7)", "pc:Poland"), ("Netherlands (3)", "pc:Netherlands"),
                            ("Ireland (1)", "pc:Ireland"), ("All (11)", "pc:all"),
                            ("This week (11)", "ps:week"), ("LinkedIn (1)", "li:next:0")]


def test_a_country_button_shows_only_that_country():
    d = make_desk()
    [card] = d.tap("pc:Netherlands")
    assert is_card(card) and card_country(d, card) == "Netherlands"
    assert ("Change country", "pc:menu") in card.buttons
    for _ in range(4):  # Next keeps to the Netherlands
        nxt = next(data for _, data in card.buttons if data.startswith("nx:"))
        [card] = d.tap(nxt)
        assert card_country(d, card) == "Netherlands"
    assert d.tap("pc:menu")[0].text == CHOOSE_COUNTRY


def test_the_next_job_after_skip_stays_in_the_country_then_offers_the_rest():
    d = make_desk()
    [card] = d.tap("pc:Ireland")
    pid = next(data for _, data in card.buttons if data.startswith("sk:")).split(":", 1)[1]
    replies = d.tap(f"sk:{pid}")
    assert replies[0].text.startswith("Skipped: ")
    assert replies[-1].text == "No more jobs for Ireland. Pick the next country:"
    assert ("Poland (7)", "pc:Poland") in replies[-1].buttons


def test_pending_with_a_country_word():
    d = make_desk()
    [card] = d.pending_command("pl")
    assert card_country(d, card) == "Poland"
    [card] = d.pending_command("all")
    assert ("Change country", "pc:menu") not in card.buttons
    assert d.pending_command("germany")[0].text.startswith("Send /pending, or /pending today")


def test_one_country_only_needs_no_menu():
    d = make_desk()
    for row in d.repo.rows.values():
        row["Country"] = "Poland"
    [card] = d.pending_command()
    assert is_card(card)


def test_labels_and_help():
    assert country_label("Other") == "Remote EU" and country_label("Poland") == "Poland"
    assert "/pending poland" in tb.HELP_TEXT


def _found(d, today_ids, week_days=3, old_days=20):
    from datetime import timedelta

    from test_review_one_at_a_time import FAKE_TODAY

    for n, (pid, row) in enumerate(d.repo.rows.items()):
        days = 0 if pid in today_ids else (week_days if n % 2 else old_days)
        row["Swept date"] = FAKE_TODAY - timedelta(days=days)


def test_bare_pending_in_telegram_shows_only_todays_jobs():
    """Phase 23 3-01 (8 Nov): /pending offered all 274 waiting jobs; today's by default."""
    d = make_desk()
    pending = [r.page_id for r in d.all_ranked()]
    _found(d, {pending[0]})
    [card] = d.pending_command("", "today")
    assert is_card(card) and card.text.startswith("[1/1]")
    assert d.chosen_scope() == "today"
    # The last of today's jobs: the other scopes are offered.
    [done] = d.tap(next(data for _, data in card.buttons if data.startswith("nx:")))
    assert is_card(done)  # Next wraps round within today's one job
    [menu] = d.tap("ps:all")
    assert d.chosen_scope() == "all"
    assert any(data == "ps:today" for _, data in menu.buttons)


def test_no_job_found_today_offers_the_older_ones():
    d = make_desk()
    _found(d, set())
    [reply] = d.pending_command("", "today")
    assert reply.text.startswith("No job found today waits for review.")
    labels = [data for _, data in reply.buttons]
    assert "ps:week" in labels and "ps:all" in labels
    week = d.pending_command("week")
    assert d.chosen_scope() == "week" and week


def test_pending_all_and_a_country_show_every_day():
    d = make_desk()
    _found(d, set())
    [card] = d.pending_command("all")
    assert is_card(card) and d.chosen_scope() == "all"
