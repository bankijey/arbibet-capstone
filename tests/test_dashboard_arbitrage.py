import pytest
from dashboard.arbitrage import stake_split


def test_every_outcome_returns_the_same() -> None:
    odds = [2.20, 1.90]
    split = stake_split(odds)
    assert split is not None
    returns = [f * o for f, o in zip(split.fractions, odds, strict=True)]
    assert returns[0] == pytest.approx(returns[1])
    assert returns[0] == pytest.approx(split.arbitrage)
    assert sum(split.fractions) == pytest.approx(1.0)
    assert split.arbitrage == pytest.approx(1 / (1 / 2.2 + 1 / 1.9))


def test_a_three_way_surebet_splits_toward_the_shortest_price() -> None:
    split = stake_split([3.20, 4.00, 3.50])
    assert split is not None and split.arbitrage > 1
    assert split.fractions[0] == max(split.fractions)


def test_a_missing_or_impossible_price_gives_no_split() -> None:
    assert stake_split([2.0, 1.0]) is None
    assert stake_split([]) is None
