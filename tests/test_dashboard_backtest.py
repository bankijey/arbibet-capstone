import pandas as pd
import pytest
from dashboard.backtest import START, choose, compare, simulate

T0 = pd.Timestamp("2026-09-10 12:00", tz="UTC")


def _det(
    event: str, minute: float, odds: float, ev: float, verdict: str | None, ko_hours: float = 6
):
    return {
        "EVENT_ID": event,
        "MARKET_ID": "18;2.5",
        "OUTCOME_ID": "12",
        "DETECTED_AT": T0 + pd.Timedelta(minutes=minute),
        "KICKOFF_AT": T0 + pd.Timedelta(hours=ko_hours),
        "ODDS": odds,
        "EV": ev,
        "VERDICT": verdict,
    }


def test_timing_picks_one_detection_per_opportunity() -> None:
    frame = pd.DataFrame(
        [
            _det("a", 0, 2.0, 0.02, "won"),
            _det("a", 30, 2.2, 0.10, "won"),
            _det("a", 60, 2.1, 0.05, "won"),
        ]
    )
    assert choose(frame, "first").ODDS.tolist() == [2.0]
    assert choose(frame, "best").ODDS.tolist() == [2.2]
    assert choose(frame, "last").ODDS.tolist() == [2.1]


def test_unsettled_detections_are_not_bet() -> None:
    frame = pd.DataFrame([_det("a", 0, 2.0, 0.05, None)])
    assert choose(frame, "first").empty


def test_flat_stake_arithmetic() -> None:
    # One unit at 2.0 wins (+1), one unit at 3.0 loses (-1).
    bets = pd.DataFrame(
        [_det("a", 0, 2.0, 0.05, "won", ko_hours=1), _det("b", 10, 3.0, 0.05, "lost", ko_hours=1)]
    )
    r = simulate(choose(bets, "first"), "flat")
    assert r.bets == 2 and r.won == 1
    assert r.profit == pytest.approx(0.0)
    assert r.final == pytest.approx(START)


def test_kelly_stakes_ev_over_net_odds_and_stakes_only_from_cash() -> None:
    # Kelly fraction = 0.10 / (2.0 - 1) = 10% of a 100 bankroll.
    bets = pd.DataFrame([_det("a", 0, 2.0, 0.10, "won", ko_hours=1)])
    r = simulate(choose(bets, "first"), "kelly")
    assert r.staked == pytest.approx(10.0)
    assert r.final == pytest.approx(110.0)


def test_a_push_returns_the_stake() -> None:
    bets = pd.DataFrame([_det("a", 0, 1.9, 0.05, "push", ko_hours=1)])
    assert simulate(choose(bets, "first"), "fixed_2pct").final == pytest.approx(START)


def test_compare_covers_every_strategy() -> None:
    bets = pd.DataFrame([_det("a", 0, 2.0, 0.05, "won", ko_hours=1)])
    table = compare(bets)
    assert len(table) == 15
    assert set(table.sizing) == {"flat", "fixed_2pct", "kelly", "half_kelly", "quarter_kelly"}
