"""The hot loop tells a dead connection from a bad fixture."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from runner import hot as hot_module
from runner.hot import HotLoop

from arbibet_capstone.fixtures import Fixture


def _loop() -> HotLoop:
    listener = SimpleNamespace(connected=True)
    return HotLoop(None, SimpleNamespace(), listener, threading.Event())


def _fixture() -> Fixture:
    kickoff = datetime.now(UTC) + timedelta(hours=3)
    return Fixture(uuid4(), kickoff, "A", "B", None, None, None, None, None)


def test_a_closed_connection_is_raised_not_counted_per_fixture(monkeypatch):
    loop = _loop()
    fixtures = [_fixture() for _ in range(3)]
    loop.watched = {f.event_id: f for f in fixtures}
    calls: list[object] = []

    def dead(conn, event_id):
        calls.append(event_id)
        raise psycopg.OperationalError("the connection is closed")

    monkeypatch.setattr(hot_module.bronze, "latest_write_times", lambda conn, ids: {})
    monkeypatch.setattr(hot_module.bronze, "latest_payloads", dead)
    with pytest.raises(psycopg.OperationalError):
        loop._recompute_all(None, {}, [f.event_id for f in fixtures], notified=True)
    assert len(calls) == 1  # stopped at the first, for the loop to reconnect


def test_a_bad_fixture_is_skipped_and_the_rest_continue(monkeypatch):
    loop = _loop()
    fixtures = [_fixture() for _ in range(3)]
    loop.watched = {f.event_id: f for f in fixtures}
    calls: list[object] = []

    def bad(conn, event_id):
        calls.append(event_id)
        raise ValueError("unparseable payload")

    monkeypatch.setattr(hot_module.bronze, "latest_write_times", lambda conn, ids: {})
    monkeypatch.setattr(hot_module.bronze, "latest_payloads", bad)
    assert loop._recompute_all(None, {}, [f.event_id for f in fixtures], notified=True) == 0
    assert len(calls) == 3 and loop._stats["failures"] == 3
