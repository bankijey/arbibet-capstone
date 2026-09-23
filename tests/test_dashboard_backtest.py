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


def test_paper_wallet_scales_with_its_start_and_a_lookback_window() -> None:
    from dashboard.backtest import paper_wallet, swings, window

    t0 = pd.Timestamp("2026-09-01 12:00", tz="UTC")
    surebets = pd.DataFrame(
        {
            "DETECTED_AT": [t0, t0 + pd.Timedelta(days=10)],
            "KICKOFF_AT": [t0 + pd.Timedelta(hours=1), t0 + pd.Timedelta(days=10, hours=1)],
            "ARBITRAGE": [1.02, 1.05],
        }
    )
    ev = pd.DataFrame(columns=["DETECTED_AT", "KICKOFF_AT", "EV", "ODDS", "VERDICT"])
    now = t0 + pd.Timedelta(days=20)
    small = paper_wallet(surebets, ev, now, start=1_000.0)
    big = paper_wallet(surebets, ev, now, start=100_000.0)
    # 20% of the bankroll at 2%, then 20% of the new bankroll at 5%: the same
    # shape at any size, so profit is proportional to the start.
    assert small.surebets == 2 and round(small.profit, 2) == round(big.profit / 100, 2)
    assert round(small.final, 2) == round(1_000 * (1 + 0.2 * 0.02) * (1 + 0.2 * 0.05), 2)
    # A window starting 12 days back leaves the first surebet out; one ending
    # before the second leaves that one out, and the wallet is valued at the end.
    later = paper_wallet(window(surebets, now - pd.Timedelta(days=12), now), ev, now, start=1_000.0)
    assert later.surebets == 1 and round(later.final, 2) == 1_010.0
    early_end = t0 + pd.Timedelta(days=5)
    early = paper_wallet(window(surebets, None, early_end), ev, early_end, start=1_000.0)
    assert early.surebets == 1 and round(early.final, 2) == 1_004.0
    assert window(surebets, None, None) is surebets
    assert list(window(surebets, t0, t0).ARBITRAGE) == [1.02]

    curve = pd.DataFrame(
        {
            "AT": [t0, t0 + pd.Timedelta(days=1), t0 + pd.Timedelta(days=2)],
            "BANKROLL": [1_050.0, 900.0, 950.0],
        }
    )
    s = swings(curve, 1_000.0)
    assert list(s.PEAK) == [1_050.0, 1_050.0, 1_050.0]
    assert [round(c, 1) for c in s.CHANGE] == [50.0, -150.0, 50.0]
    assert round(float(s.DRAWDOWN.max()), 4) == round(150 / 1_050, 4)
    assert swings(curve.iloc[:0], 1_000.0).empty


def test_a_surebet_with_no_free_cash_is_skipped_not_counted() -> None:
    from dashboard.backtest import paper_wallet

    t0 = pd.Timestamp("2026-09-01 12:00", tz="UTC")
    tied = pd.DataFrame(
        [
            # The first surebet takes the whole bankroll and settles in two days;
            # the second arrives an hour later with nothing left to stake.
            {"DETECTED_AT": t0, "KICKOFF_AT": t0 + pd.Timedelta(days=2), "ARBITRAGE": 1.02},
            {
                "DETECTED_AT": t0 + pd.Timedelta(hours=1),
                "KICKOFF_AT": t0 + pd.Timedelta(days=2),
                "ARBITRAGE": 1.05,
            },
        ]
    )
    ev = pd.DataFrame(columns=["DETECTED_AT", "KICKOFF_AT", "EV", "ODDS", "VERDICT"])
    w = paper_wallet(tied, ev, t0 + pd.Timedelta(days=3), start=1_000.0, surebet_fraction=1.0)
    assert w.surebets == 1 and len(w.entries) == 1 and w.open_bets == 0
    assert round(w.final, 2) == 1_020.0
