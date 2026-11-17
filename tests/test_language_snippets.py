"""Polish and Dutch jobs that came in as Adzuna snippets were saved (1 Oct test T6)."""

from jobengine.sweep.fit import language_block, title_written_in

DUTCH_SNIPPET = ("Wij zoeken een DevOps engineer voor het platformteam van het Openbaar "
                 "Ministerie. Je werkt met de nieuwste tools en bent verantwoordelijk voor de "
                 "CI/CD straat.")
ENGLISH_SNIPPET = ("We are looking for a DevOps engineer to join the team in Warsaw and work "
                   "with Kubernetes and the cloud. You will be part of the platform.")


def test_polish_and_dutch_titles():
    assert title_written_in("Inżynier DevOps (k/m)") == "Polish"
    assert title_written_in("Specjalista ds. DevOps") == "Polish"
    assert title_written_in("Cloud Beheerder") == "Dutch"
    for english in ("DevOps Engineer - Gdańsk", "DevOps Engineer (m/v/x)",
                    "Site Reliability Engineer (k/m)", "Senior Platform Engineer"):
        assert title_written_in(english) is None, english


def test_short_snippets_are_judged_too():
    assert language_block(DUTCH_SNIPPET, "DevOps / CI/CD Engineer - Platformteam") == "Dutch"
    assert language_block(ENGLISH_SNIPPET, "DevOps Engineer") is None
    assert language_block(None, "Inżynier DevOps (k/m)") == "Polish"
