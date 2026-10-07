"""Only surebets are pushed to Telegram unless TELEGRAM_EV_ALERTS=1."""

from __future__ import annotations

import threading

import pytest
from runner.telegram.alerts import Alerter, ev_alerts_enabled

SUREBET = [{"arbitrage": 1.02}]
EV = [{"ev": 0.04}]


def _alerter() -> Alerter:
    return Alerter(api=None, store=None, warehouse=None, stats={}, stop=threading.Event())  # type: ignore[arg-type]


def test_ev_is_not_queued_by_default_and_surebets_still_are(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_EV_ALERTS", raising=False)
    assert not ev_alerts_enabled()
    alerter = _alerter()
    alerter.submit("fixture", None, [], EV)  # type: ignore[arg-type]
    assert alerter.queue.empty()  # an EV-only recompute sends nothing
    alerter.submit("fixture", None, SUREBET, EV)  # type: ignore[arg-type]
    _, _, arb, ev, _ = alerter.queue.get_nowait()
    assert arb == SUREBET and ev == []  # the surebet goes; its EV rows are dropped


def test_ev_alerts_return_when_switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_EV_ALERTS", "1")
    alerter = _alerter()
    alerter.submit("fixture", None, [], EV)  # type: ignore[arg-type]
    _, _, _, ev, _ = alerter.queue.get_nowait()
    assert ev == EV
