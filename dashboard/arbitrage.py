"""Arbitrage arithmetic for the bet-sizing tool. Pure, so it is tested."""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple


class Split(NamedTuple):
    arbitrage: float  # return per unit staked, whatever wins
    fractions: list[float]  # share of the stake per leg, summing to 1


def stake_split(odds: Sequence[float]) -> Split | None:
    """How to divide one stake across every outcome so each pays the same.

    Stake on outcome i is `(1/odds_i) / sum(1/odds_j)`. Whichever outcome
    lands, the return is stake x arbitrage, where arbitrage is
    `1 / sum(1/odds_j)`: above 1 is a guaranteed profit, below 1 a guaranteed
    loss. None when any price is not a real price.
    """
    if not odds or any(o is None or o <= 1 for o in odds):
        return None
    inverse = [1.0 / o for o in odds]
    total = sum(inverse)
    return Split(arbitrage=1.0 / total, fractions=[i / total for i in inverse])
