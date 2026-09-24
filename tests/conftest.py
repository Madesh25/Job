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
