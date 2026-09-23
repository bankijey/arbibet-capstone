"""The bet wallet: sizing from balances, settlement, the paper wallet."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from dashboard.backtest import PAPER_START, paper_wallet
from runner.telegram import render
from runner.telegram.model import Leg as OppLeg
from runner.telegram.model import Opportunity
from runner.telegram.wallet import (
    Leg,
    bet_row,
    guaranteed,
    parse_stakes,
    settle,
    size_ev,
    size_surebet,
    summary,
)

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def test_surebet_sizing_is_capped_by_the_smallest_book():
    # The Levski bet: 2.10 at livescorebet, 1.98 at msport. Fractions 48.5% / 51.5%.
    odds, books = [2.10, 1.98], ["livescorebet", "msport"]
    s = size_surebet(odds, books, {"livescorebet": 4_700, "msport": 50_000}, cap=100_000)
    assert s.limited_by == "livescorebet"
    assert s.stakes[0] == pytest.approx(4_700)
    assert s.total == pytest.approx(4_700 / (1 / 2.10) * (1 / 2.10 + 1 / 1.98))
    # Both legs return the same whichever lands.
    assert s.stakes[0] * 2.10 == pytest.approx(s.stakes[1] * 1.98)
    # Rich at both books: the subscriber's cap binds instead.
    s = size_surebet(odds, books, {"livescorebet": 1e6, "msport": 1e6}, cap=10_000)
    assert s.total == 10_000 and s.limited_by is None
    # No balance at a book: nothing can be staked, and the book is named.
    s = size_surebet(odds, books, {"livescorebet": 5_000}, cap=10_000)
    assert s.total == 0 and s.short == ["msport"]


def test_ev_sizing_is_quarter_kelly_within_the_book():
    s = size_ev(0.04, 2.0, "msport", {"msport": 1_000, "bet9ja": 9_000}, 10_000, cap=5_000)
    assert s.total == pytest.approx(10_000 * 0.04 / 1.0 / 4)  # 100
    s = size_ev(0.40, 2.0, "msport", {"msport": 500, "bet9ja": 9_500}, 10_000, cap=5_000)
    assert s.total == 500 and s.limited_by == "msport"
    assert size_ev(0.0, 2.0, "msport", {}, 1, 1) is None


def test_settlement_needs_every_leg_and_pays_each_verdict():
    legs = [
        Leg("livescorebet", "12", "over", 2.10, 4_700),
        Leg("msport", "13", "under", 1.98, 5_000),
    ]
    assert guaranteed(legs) == pytest.approx(min(4_700 * 2.10, 5_000 * 1.98))
    complete, payout = settle(legs, {"12": "won"})
    assert not complete and payout == 0
    complete, payout = settle(legs, {"12": "lost", "13": "won"})
    assert complete and payout == pytest.approx(5_000 * 1.98)
    assert legs[0].payout == 0 and legs[1].verdict == "won"
    # A voided leg returns its stake: the guarantee is gone, the arithmetic is honest.
    legs = [Leg("a", "1", "x", 2.0, 100), Leg("b", "2", "y", 2.0, 100)]
    assert settle(legs, {"1": "void", "2": "lost"}) == (True, 100)


def test_wallet_summary_and_stake_parsing():
    bets = [
        {
            "status": "open",
            "kind": "surebet",
            "mode": "real",
            "profit": None,
            "legs": [
                {"book": "a", "outcomeId": "1", "odds": 2.1, "stake": 48.5},
                {"book": "b", "outcomeId": "2", "odds": 1.98, "stake": 51.5},
            ],
        },
        {
            "status": "settled",
            "kind": "ev",
            "mode": "real",
            "profit": 30.0,
            "legs": [{"book": "a", "outcomeId": "1", "odds": 2.5, "stake": 20}],
        },
        {
            "status": "settled",
            "kind": "ev",
            "mode": "real",
            "profit": -10.0,
            "legs": [{"book": "a", "outcomeId": "1", "odds": 2.5, "stake": 10}],
        },
    ]
    s = summary(bets, {"a": 1_000, "b": 500})
    assert s["open_bets"] == 1 and s["open_stake"] == 100
    assert s["locked_profit"] == pytest.approx(min(48.5 * 2.1, 51.5 * 1.98) - 100)
    assert s["equity"] == 1_600 and s["settled_won"] == 1 and s["profit"] == 20
    assert s["roi"] == pytest.approx(20 / 30)
    assert parse_stakes(["4,700", "5000"], 2) == [4_700, 5_000]
    assert parse_stakes(["x"], 1) is None and parse_stakes(["1"], 2) is None


def _opp(**kw) -> Opportunity:
    base = dict(
        kind="surebet",
        event_id="e",
        market_id="18;2.5",
        fixture="A v B",
        tournament=None,
        kickoff=NOW + timedelta(days=1),
        market="Over/Under 2.5",
        value=1.0191,
        legs=(
            OppLeg("Over 2.5", "livescorebet", 2.10, None, "12"),
            OppLeg("Under 2.5", "msport", 1.98, None, "13"),
        ),
        detected_at=NOW,
        spread_seconds=0,
    )
    return Opportunity(**{**base, **kw})


def test_bet_row_and_alert_rendering_by_access():
    opp = _opp()
    legs = [
        Leg("livescorebet", "12", "Over 2.5", 2.10, 4_700),
        Leg("msport", "13", "Under 2.5", 1.98, 5_000),
    ]
    row = bet_row(7, "real", "surebet", opp, legs, NOW, "button")
    assert row["opportunity_key"] == opp.key and row["legs"][1]["outcomeId"] == "13"
    # A reader without balances sees prices and the nudge, never a stake.
    public = render.alert(opp, NOW, None)
    assert "stake" not in public.split("Stakes sized")[0] and "/balance" in public
    # A signed reader sees stakes from their sizing and what limited them.
    sizing = size_surebet(
        [2.10, 1.98], ["livescorebet", "msport"], {"livescorebet": 4_700, "msport": 50_000}, 100_000
    )
    signed = render.alert(opp, NOW, sizing)
    assert "stake <b>4,700</b>" in signed and "limited by your livescorebet" in signed
    assert "💰 Stake 9,6" in signed and "whatever wins (+" in signed
    text = render.bet_summary({**row, "status": "open", "kickoff_at": opp.kickoff})
    assert "locks in" in text
    wallet = render.wallet(
        "real",
        {"msport": 45_000},
        summary([{**row, "status": "open", "bet_id": 1}], {"msport": 45_000}),
        [],
    )
    assert "Locked-in profit" in wallet


def test_paper_wallet_compounds_surebets_and_settles_ev():
    surebets = pd.DataFrame(
        [
            {"DETECTED_AT": NOW, "KICKOFF_AT": NOW + timedelta(hours=3), "ARBITRAGE": 1.02},
            {
                "DETECTED_AT": NOW + timedelta(hours=6),
                "KICKOFF_AT": NOW + timedelta(hours=9),
                "ARBITRAGE": 1.05,
            },
        ]
    )
    ev = pd.DataFrame(
        [
            {
                "DETECTED_AT": NOW + timedelta(hours=1),
                "KICKOFF_AT": NOW + timedelta(hours=3),
                "EV": 0.10,
                "ODDS": 2.0,
                "VERDICT": "won",
            },
        ]
    )
    w = paper_wallet(surebets, ev, NOW + timedelta(days=1))
    # First surebet: 20% of the start at 1.02 -> +0.4%, still open when the EV bet is
    # placed, so that bankroll is 80% cash + 20.4% due. Quarter-Kelly of it at
    # EV 0.10 on evens is 2.5%, won at 2.0. The second surebet then
    # compounds on everything settled.
    ev_stake = (PAPER_START - PAPER_START * 0.20 + PAPER_START * 0.20 * 1.02) * 0.025
    second = (PAPER_START * 1.004 + ev_stake) * 0.20 * 0.05
    assert w.surebets == 2 and w.ev_bets == 1 and w.ev_won == 1
    assert w.final == pytest.approx(PAPER_START * 1.004 + ev_stake + second)
    assert w.open_bets == 0 and w.max_drawdown == 0
    empty = paper_wallet(surebets.iloc[:0], ev.iloc[:0], NOW)
    assert empty.final == PAPER_START and empty.curve.empty
