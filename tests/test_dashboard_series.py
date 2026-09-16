import pandas as pd
from dashboard.series import arbitrage_series, latest_odds, reduce_steps

T0 = pd.Timestamp("2026-09-15 12:00", tz="UTC")


def _at(minutes: float) -> pd.Timestamp:
    return T0 + pd.Timedelta(minutes=minutes)


def _ticks(rows: list[tuple[str, str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [(o, b, odds, _at(m)) for o, b, odds, m in rows],
        columns=["OUTCOME_ID", "BOOKMAKER_NAME", "ODDS", "FIRE_TIME"],
    )


# --- reduce_steps -----------------------------------------------------------------


def test_a_long_series_keeps_its_largest_moves_first_and_last() -> None:
    # 100 one-rung wiggles around 2.00, one real move to 3.00 at minute 50.
    odds = [2.00 + (0.01 if i % 2 else 0.0) for i in range(100)]
    odds[50] = 3.00
    df = pd.DataFrame({"AT": [_at(i) for i in range(100)], "ODDS": odds, "BOOK": "a"})

    kept = reduce_steps(df, x="AT", y="ODDS", by=["BOOK"], points=10)

    assert len(kept) <= 10
    assert kept.AT.iloc[0] == _at(0) and kept.AT.iloc[-1] == _at(99)
    assert 3.00 in kept.ODDS.to_list()


def test_a_short_series_is_untouched() -> None:
    df = pd.DataFrame({"AT": [_at(i) for i in range(5)], "ODDS": [1, 2, 3, 4, 5]})
    assert len(reduce_steps(df, x="AT", y="ODDS", points=10)) == 5


def test_each_series_is_reduced_on_its_own() -> None:
    df = pd.DataFrame(
        {
            "AT": [_at(i) for i in range(50)] * 2,
            "ODDS": [2.0 + (i % 7) / 10 for i in range(100)],
            "BOOK": ["a"] * 50 + ["b"] * 50,
        }
    )
    kept = reduce_steps(df, x="AT", y="ODDS", by=["BOOK"], points=10)
    assert kept.groupby("BOOK").size().max() <= 10
    assert set(kept.BOOK) == {"a", "b"}


def test_a_marked_moment_stays_on_the_line() -> None:
    # A detection at minute 30.5 must be drawn on the price that stood then,
    # which is the tick at minute 30 -- even though it is a tiny move.
    odds = [2.0] * 60
    odds[30] = 2.01
    odds[45] = 5.0
    df = pd.DataFrame({"AT": [_at(i) for i in range(60)], "ODDS": odds})

    kept = reduce_steps(df, x="AT", y="ODDS", points=3, keep_x=[_at(30.5)])

    assert _at(30) in kept.AT.to_list()


# --- arbitrage_series -------------------------------------------------------------


def test_the_arbitrage_opens_and_closes() -> None:
    ticks = _ticks(
        [
            ("over", "a", 1.90, 0),
            ("under", "b", 1.90, 0),  # 1/(1/1.9 + 1/1.9) = 0.95
            ("under", "b", 2.20, 10),  # 1/(1/1.9 + 1/2.2) = 1.0195 -- open
            ("under", "b", 1.95, 20),  # 0.9623 -- closed again
        ]
    )

    series = arbitrage_series(ticks, outcomes=["over", "under"])

    assert [round(v, 4) for v in series.ARBITRAGE] == [0.95, 1.0195, 0.9623]


def test_a_moment_missing_an_outcome_is_skipped() -> None:
    ticks = _ticks([("over", "a", 1.9, 0), ("under", "b", 2.0, 5)])
    series = arbitrage_series(ticks, outcomes=["over", "under"])
    assert series.FIRE_TIME.to_list() == [_at(5)]


def test_the_spread_exposes_a_price_carried_forward() -> None:
    # Book a last changed at minute 0; book b at minute 90. The arbitrage at 90
    # rests on a price 90 minutes old.
    ticks = _ticks([("over", "a", 2.2, 0), ("under", "b", 1.5, 1), ("under", "b", 2.1, 90)])
    last = arbitrage_series(ticks, outcomes=["over", "under"]).iloc[-1]
    assert last.SPREAD_SECONDS == 90 * 60


def test_until_cuts_the_history_at_kick_off() -> None:
    ticks = _ticks([("over", "a", 1.9, 0), ("under", "b", 1.9, 0), ("under", "b", 3.0, 120)])
    series = arbitrage_series(ticks, outcomes=["over", "under"], until=_at(60))
    assert len(series) == 1 and round(series.ARBITRAGE.iloc[0], 2) == 0.95


def test_latest_odds_respects_until() -> None:
    ticks = _ticks([("over", "a", 1.9, 0), ("over", "a", 2.5, 120)])
    assert latest_odds(ticks, until=_at(60)).ODDS.to_list() == [1.9]
    assert latest_odds(ticks).ODDS.to_list() == [2.5]
