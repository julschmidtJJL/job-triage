"""No live network in tests: every fetcher is exercised against fixtures."""
import socket

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("network access in tests")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(autouse=True)
def _default_board(monkeypatch):
    """A BOARD set in your shell must not send the tests' jobs to your board."""
    monkeypatch.delenv("BOARD", raising=False)
    monkeypatch.delenv("BOARD_DIR", raising=False)


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    """Scoring retries wait 10-60 s in real runs; not in tests."""
    import utils.pipeline as pl
    monkeypatch.setattr(pl, "_sleep", lambda seconds: None)
    monkeypatch.delenv("SCORING_FAIL_AFTER_BATCHES", raising=False)
