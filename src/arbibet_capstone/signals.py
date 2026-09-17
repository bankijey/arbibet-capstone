"""Turning engine output into warehouse rows.

The vendored arbitrage engine returns pandas-shaped records; the warehouse
wants flat rows with a stable key and the leg-freshness columns. That
translation lives here rather than in the consumers, because it is the part
worth testing and the consumers are wiring.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, NamedTuple

from arbibet_capstone.crosswalk.arbitrage import combine_outcomes, compute_arbitrage
from arbibet_capstone.snapshot import Snapshot

# Markets the vendored engine's author excluded as unreliable, kept so that
# behaviour is not quietly widened here.
_EXCLUDED_MARKETS = frozenset({"60010", "60011", "60012"})

# The widest gap, in seconds, allowed between the oldest and newest price in an
# arbitrage. An arbitrage claims its prices are buyable AT THE SAME TIME; a
# snapshot only guarantees they were RECORDED, each book at its own last
# publish. Across 449 recorded signals, not one with legs inside five minutes
# was physically implausible (worst 1.059), while all 48 implausible ones sat
# above it -- the worst at 1.6788, a first-half 1X2 whose three prices were 78
# minutes apart and whose implied probabilities summed to 0.596. See FINDINGS
# 13g. Mirrored by the dbt var `max_leg_spread_seconds`.
MAX_LEG_SPREAD_SECONDS = 300


def _split_market(market_id: str) -> tuple[int, str | None]:
    """`"18;2.5"` -> `(18, "2.5")`; `"1"` -> `(1, None)`.

    Split on the FIRST separator only: bet9ja writes market 14's specifier as
    `1:0`, and a naive split would lose half of it.
    """
    base, _, specifier = market_id.partition(";")
    return int(base), specifier or None


def _signal_key(event_id: str, market_id: str, legs: list[dict[str, Any]]) -> str:
    """A stable identity for one detected signal.

    The legs are part of the key on purpose. Re-processing the same message
    converges on the same row, which is what makes the producer re-runnable.
    A genuinely different price picture for the same market is a different
    observation and earns its own row, so the table stays a log rather than one
    mutable current-state row per market.
    """
    material = "|".join(
        [event_id, market_id]
        + sorted(
            "{}:{}:{}".format(leg["outcome_id"], leg["bookmaker"], leg["odds"]) for leg in legs
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def arbitrage_rows(
    snapshot: Snapshot,
    threshold: float = 1.0,
    max_leg_spread_seconds: int = MAX_LEG_SPREAD_SECONDS,
) -> list[dict[str, Any]]:
    """`fact_arbitrage_signal` rows for one snapshot.

    `threshold` below 1.0 records near-arbitrage: markets where the best
    available prices across books fall short of a guaranteed return, but only
    just. That shortfall is the cross-book disagreement this platform measures,
    and with true surebets close to nonexistent it is what the table mostly
    holds. The `arbitrage` column says which a row is: above 1.0 is a surebet.
    """
    if len(snapshot.books) < 2:
        # An arbitrage needs two books to disagree. One cannot.
        return []

    fire_times = {book: quote.fire_time for book, quote in snapshot.books.items()}
    records = compute_arbitrage(
        {book: quote.markets for book, quote in snapshot.books.items()},
        threshold=threshold,
    )

    rows: list[dict[str, Any]] = []
    for record in records:
        arbitrage = record.get("arbitrage")
        market_id = record["marketId"]
        if arbitrage is None or market_id in _EXCLUDED_MARKETS:
            continue

        legs = [
            {"outcome_id": leg["id"], "bookmaker": leg["bookmaker"], "odds": leg["odds"]}
            for group in record["results"]
            for leg in group["results"]
        ]
        if len(legs) < 2:
            continue
        if len({leg["bookmaker"] for leg in legs}) < 2:
            # The snapshot has two books, but THIS market may be priced by
            # only one of them -- and then the engine builds every leg from
            # that book. Two outcomes of one book are not a cross-book
            # disagreement; they are that book's own overround, which at 0.98
            # or so sails through the near-arbitrage threshold as a "signal".
            # sportybet alone on market 60210 for Anderlecht v Kortrijk: 1.15
            # and 7.10, "arbitrage 0.9897", meaningless. It also polluted
            # gold_market_efficiency with single-book vig, the one thing that
            # model's docstring says it does not measure.
            continue

        # Bronze writes only on change, so the books' latest payloads differ in
        # age. The spread separates a tradeable signal from an artefact of
        # stale legs, and it is knowable here only because fire_time travelled
        # with each payload from the bronze reader onward.
        times = [fire_times[leg["bookmaker"]] for leg in legs]
        oldest, newest = min(times), max(times)
        spread = int((newest - oldest).total_seconds())
        if spread > max_leg_spread_seconds:
            # Refused, not flagged. A stale leg does not make a weaker
            # arbitrage; it makes a number that is not an arbitrage at all, and
            # every consumer downstream would have to remember to filter it.
            continue

        base, specifier = _split_market(market_id)
        rows.append(
            {
                "signal_key": _signal_key(str(snapshot.fixture.event_id), market_id, legs),
                "event_id": str(snapshot.fixture.event_id),
                "market_id": market_id,
                "market_base_id": base,
                "specifier": specifier,
                "arbitrage": float(arbitrage),
                "n_legs": len(legs),
                "legs": legs,
                "oldest_leg_fire_time": oldest,
                "newest_leg_fire_time": newest,
                "leg_spread_seconds": spread,
                # When the price picture existed, not when a consumer got to
                # it. The newest leg is the moment the whole set was last
                # observable. Consumer time was used before, and a snapshot
                # published before kick-off but consumed after it read as a
                # post-kick-off detection -- the misreading behind FINDINGS 13f.
                "detected_at": newest,
            }
        )
    return rows


# --- positive expected value ------------------------------------------------
#
# EV is a claim about ONE outcome at ONE book: this price is longer than the
# outcome's true chance deserves. So the grain is the outcome, not the market
# the way arbitrage is.
#
# Only two books publish a probability with their prices, so every other book's
# EV is computed against a BORROWED probability. Which book lent it is
# therefore part of the answer, not an implementation detail -- an EV number
# with no provenance cannot be audited six months later, which is why
# `p_source` is on the fact.
#
# WHICH probability. Both publishing books are candidates, and the one used is
# whichever published MOST RECENTLY for this fixture -- by payload fire_time,
# the moment the book last confirmed that number. It used to be a fixed
# priority, sportybet over msport, regardless of age: a sportybet probability
# from two hours ago beat an msport one from two minutes ago, and the EV was
# computed against the stale one. On an exact tie, sportybet still wins, so the
# choice is always deterministic.
#
# fire_time rather than the per-outcome `lastChange`: a payload fired at T says
# what the book's probability WAS at T, even if it last changed hours earlier.
# Unchanged is not stale; unobserved is.
#
# The vendored `fill_probabilities` did a join like this too, but discarded the
# source and ordered by bookmaker name, so msport won alphabetically while its
# docstring claimed sportybet had priority.
_PROBABILITY_PRIORITY = ("sportybet", "msport")

# The widest gap allowed between the price and the probability it is judged
# against. EV is a claim that THIS price beats THIS probability now; if they
# were published minutes apart, one of them may already have moved. The same
# bar as arbitrage legs (MAX_LEG_SPREAD_SECONDS), where it was measured -- EV
# rows never recorded the probability's age before, so there is no EV history
# to measure it on yet. `probability_spread_seconds` is recorded from now on
# so there will be.
MAX_PROBABILITY_SPREAD_SECONDS = MAX_LEG_SPREAD_SECONDS


class _Probability(NamedTuple):
    p: float
    source: str
    fire_time: datetime


def _probability_sources(
    outcomes: Any, fire_times: dict[str, datetime]
) -> dict[tuple[str, str], _Probability]:
    """`(market_id, outcome_id) -> the most recently published probability`."""
    sources: dict[tuple[str, str], _Probability] = {}
    for book in _PROBABILITY_PRIORITY:
        if book not in fire_times:
            continue
        fired = fire_times[book]
        rows = outcomes[(outcomes["bookmaker"] == book) & outcomes["p"].notna()]
        for row in rows.itertuples(index=False):
            key = (row.marketId, row.id)
            current = sources.get(key)
            # Strictly newer replaces. Priority order of iteration settles a
            # tie: sportybet is seen first and an equal msport time does not
            # displace it.
            if current is None or fired > current.fire_time:
                sources[key] = _Probability(float(row.p), book, fired)
    return sources


def ev_rows(
    snapshot: Snapshot,
    min_ev: float = 0.01,
    min_probability: float = 0.5,
    max_probability_spread_seconds: int = MAX_PROBABILITY_SPREAD_SECONDS,
) -> list[dict[str, Any]]:
    """`fact_ev_signal` rows for one snapshot.

    `min_probability` keeps the vendored engine's filter. It excludes longshots,
    where a small absolute error in an estimated probability swamps the edge:
    at p=0.05 a two-point misestimate moves EV by 40% of stake, at p=0.6 by 3%.
    A configurable floor, not a law -- but a stated one.
    """
    if not snapshot.books:
        return []

    fire_times = {book: quote.fire_time for book, quote in snapshot.books.items()}
    outcomes = combine_outcomes({book: quote.markets for book, quote in snapshot.books.items()})
    if len(outcomes) == 0:
        return []

    sources = _probability_sources(outcomes, fire_times)
    event_id = str(snapshot.fixture.event_id)

    rows: list[dict[str, Any]] = []
    for row in outcomes.itertuples(index=False):
        if row.odds is None or row.odds <= 0:
            # A suspended outcome carries a zero price, not a free bet.
            continue
        found = sources.get((row.marketId, row.id))
        if found is None:
            # No book that publishes probabilities priced this outcome.
            continue

        probability, source, probability_fired = found
        price_fired = fire_times[row.bookmaker]
        spread = int(abs((price_fired - probability_fired).total_seconds()))
        if spread > max_probability_spread_seconds:
            # Refused, like a stale arbitrage leg: a price and a probability
            # that were never current together are not an edge.
            continue
        ev = probability * float(row.odds) - 1
        if ev < min_ev or probability < min_probability:
            continue

        base, specifier = _split_market(row.marketId)
        legs = [{"outcome_id": row.id, "bookmaker": row.bookmaker, "odds": float(row.odds)}]
        rows.append(
            {
                "signal_key": _signal_key(event_id, row.marketId, legs),
                "event_id": event_id,
                "market_id": row.marketId,
                "market_base_id": base,
                "specifier": specifier,
                "outcome_id": row.id,
                "outcome_name": row.name,
                "bookmaker": row.bookmaker,
                "odds": float(row.odds),
                "implied_p": probability,
                "p_source": source,
                "ev": ev,
                "payload_fire_time": price_fired,
                "probability_fire_time": probability_fired,
                "probability_spread_seconds": spread,
                # Market time: the moment BOTH numbers existed, which is the
                # later of the two.
                "detected_at": max(price_fired, probability_fired),
            }
        )
    return rows
