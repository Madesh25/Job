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
    assert menu.buttons == [("PL 7", "pc:Poland"), ("NL 3", "pc:Netherlands"),
                            ("IE 1", "pc:Ireland"), ("All (11)", "pc:all"),
                            ("Last 3 days (1)", "ps:week"), ("No description (1)", "jd:menu")]


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
    assert ("PL 7", "pc:Poland") in replies[-1].buttons


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


def _found(d, today_ids, week_days=2, old_days=20):
    from datetime import timedelta

    from test_review_one_at_a_time import FAKE_TODAY

    for n, (pid, row) in enumerate(d.repo.rows.items()):
        days = 0 if pid in today_ids else (week_days if n % 2 else old_days)
        row["First seen"] = FAKE_TODAY - timedelta(days=days)
        row["Swept date"] = FAKE_TODAY  # seen again today: not "today" in /pending
        row["Posted date"] = FAKE_TODAY - timedelta(days=days)


def test_bare_pending_in_telegram_shows_the_countries_then_today():
    """Phase 23 3-01 (8 Nov): today's jobs by default. 12 Nov: the country first, then Today
    (found today), Last 3 days and Everything inside that country."""
    d = make_desk()
    first = d.all_ranked()[0]
    _found(d, {first.page_id})
    [menu] = d.pending_command("", "today")
    assert menu.text.startswith("Jobs found today. ") and d.chosen_scope() == "today"
    labels = dict((data, label) for label, data in menu.buttons)
    country = first.country
    short = {"Poland": "PL", "Netherlands": "NL", "Ireland": "IE"}[country]
    assert labels[f"pc:{country}"] == f"{short} 1"
    assert all(f"pc:{c}" in labels for c in ("Poland", "Netherlands", "Ireland"))
    [card] = d.tap(f"pc:{country}")
    assert is_card(card) and card.text.startswith("[1/1]")
    # 5-01 (11 Nov): the older jobs one tap away, now of the same country.
    scopes = [data for _, data in card.buttons if data.startswith("ps:")]
    assert scopes == ["ps:week", "ps:all"] and ("Change country", "pc:menu") in card.buttons
    [done] = d.tap(next(data for _, data in card.buttons if data.startswith("nx:")))
    assert is_card(done)  # Next wraps round within today's one job
    [card] = d.tap("ps:all")
    assert d.chosen_scope() == "all" and d.chosen_country() == country
    assert is_card(card) and card_country(d, card) == country
    assert any(data == "ps:today" for _, data in card.buttons)


def test_no_job_found_today_offers_the_older_ones():
    d = make_desk()
    _found(d, set())
    [menu] = d.pending_command("", "today")
    assert ("IE 0", "pc:Ireland") in menu.buttons
    [reply] = d.tap("pc:Ireland")
    assert reply.text.startswith("No more jobs for Ireland found today. Older jobs there:")
    labels = [data for _, data in reply.buttons]
    assert "ps:all" in labels and "pc:menu" in labels
    week = d.pending_command("week")
    assert d.chosen_scope() == "week" and week


def test_pending_all_and_a_country_show_every_day():
    d = make_desk()
    _found(d, set())
    [card] = d.pending_command("all")
    assert is_card(card) and d.chosen_scope() == "all"


def test_today_means_found_today_like_the_notion_view():
    """12 Nov, your rule: Today is what today's /fetch found (the Notion Today view groups by
    the Swept date); 10 Nov it was the posted day."""
    from datetime import timedelta

    from test_review_one_at_a_time import FAKE_TODAY

    from jobengine.screen.desk import pending_in_scope
    from jobengine.screen.runner import job_row

    def row(first, posted=FAKE_TODAY - timedelta(days=5)):
        return job_row("p", {"Posted date": posted, "First seen": first,
                             "Swept date": FAKE_TODAY})

    yesterday = FAKE_TODAY - timedelta(days=1)
    assert pending_in_scope(row(FAKE_TODAY), "today", FAKE_TODAY)  # found today, posted earlier
    assert not pending_in_scope(row(yesterday), "today", FAKE_TODAY)
    assert pending_in_scope(row(yesterday), "week", FAKE_TODAY)
    assert pending_in_scope(row(FAKE_TODAY - timedelta(days=2)), "week", FAKE_TODAY)
    assert not pending_in_scope(row(FAKE_TODAY - timedelta(days=3)), "week", FAKE_TODAY)
    # 12 Nov: seen again today (Swept today) but first found 2 days ago: not Today.
    assert not pending_in_scope(row(FAKE_TODAY - timedelta(days=2)), "today", FAKE_TODAY)
    no_first = job_row("p", {"Posted date": yesterday, "Swept date": FAKE_TODAY})
    assert pending_in_scope(no_first, "today", FAKE_TODAY)  # no First seen: the Swept date
    assert pending_in_scope(row(FAKE_TODAY - timedelta(days=40)), "all", FAKE_TODAY)
