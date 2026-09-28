import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never touch the network: any real socket connection fails the test."""

    def refuse(*args, **kwargs):
        raise AssertionError("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(autouse=True)
def any_posting_age(request, monkeypatch):
    """The sweep fixtures carry fixed posted dates, so tests keep postings of any age unless
    they are marked `posting_age` (tests of sweep.max_posted_age_days itself)."""
    if request.node.get_closest_marker("posting_age") is None:
        monkeypatch.setattr("jobengine.sweep.runner.max_posted_age", lambda s: None)


@pytest.fixture(autouse=True)
def no_skill_match(request, monkeypatch):
    """The sweep fixtures' counts predate sweep.min_skill_match, so tests skip that check
    unless they are marked `skill_match` (tests of the check itself)."""
    if request.node.get_closest_marker("skill_match") is None:
        monkeypatch.setattr("jobengine.sweep.runner.skill_share", lambda job, rules: None)
