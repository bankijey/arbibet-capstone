"""Turn a fixture's bronze payload history into a series of price changes.

The producer asks bronze "what is the price now". This asks "how did it get
there", and gets the answer from the same table without any new collection:
`bronze_event_payloads` is append-only and writes a row whenever a book's
response changes, so the history is already there.

Two decisions live here, and both are about what a "tick" is.

**A tick is a price change, not a payload.** Bronze writes when ANY part of a
book's response moves -- a different market, a scoreline, a suspension flag --
so consecutive payloads routinely carry an identical price for the outcome
being charted. Emitting one row per payload would multiply the table several
times over and, worse, draw a step chart with invisible steps at every
timestamp. `price_changes` emits a row only when the number actually moves.

**The first observation is always a tick.** A price we have never seen before
is news even if the book had been quoting it for hours before bronze's first
row. Suppressing it would leave a series that begins in mid-air.
"""

from __future__ import annotations

import json
from collections.abc import Container, Iterable, Mapping
from datetime import datetime
from typing import Any, NamedTuple

from arbibet_capstone.bronze import HistoricPayload
from arbibet_capstone.crosswalk.parsers import PARSER_REGISTRY, parse_bookmaker
from arbibet_capstone.priority import yield_to_hot


class Tick(NamedTuple):
    """One outcome's price at one book becoming a new number at one moment.

    `outcome_name` is what the BOOK called it, carried through because it is
    the only honest source of a label for markets the settlement taxonomy does
    not cover. `dim_market_outcome` is materialised from the settlement
    engine's maps, and the engine settles from scores, so corners markets have
    ids and no names -- which is how the surebets table came to display
    "outcome 12". Betradar outcome ids are MARKET-SCOPED (id 12 is `over` in
    one market and `1-2` in another), so the id cannot be resolved alone. The
    books publish the name in the payload; this keeps it.
    """

    bookmaker: str
    market_id: str
    outcome_id: str
    outcome_name: str | None
    odds: float
    fire_time: datetime


class Extraction(NamedTuple):
    """The ticks found, and how many payloads could not be read.

    The count is returned rather than logged so the caller cannot ignore it
    without saying so. A backfill that quietly parsed half its input would
    draw a chart with half the movement missing and no sign anything was
    wrong.
    """

    ticks: list[Tick]
    failed_payloads: int


def price_changes(
    history: Iterable[HistoricPayload],
    wanted_markets: Container[str],
    mappings: Any,
    seed: Mapping[tuple[str, str, str], float] | None = None,
) -> Extraction:
    """The price changes in `history`, restricted to `wanted_markets`.

    `history` must be ordered oldest-first within each bookmaker, which is what
    `bronze.payload_history` returns. Order across bookmakers does not matter:
    each book's series is tracked independently.

    `wanted_markets` holds BASE betradar market ids -- '18', not '18;2.5'.
    Matching on the base rather than the full id pulls every LINE of a market
    rather than only the one a signal fired on, which is what lets the chart
    offer Over/Under 2.5 and 3.5 side by side. Scoping to a handful of base
    markets is still what keeps this to thousands of rows rather than hundreds
    of thousands.

    `seed` carries the last price already known for each
    `(bookmaker, market_id, outcome_id)`, and it is what makes an INCREMENTAL
    run correct. This function emits a tick only when a price differs from the
    previous one it saw; starting mid-history with no memory, the first payload
    after the cursor always looks like a change, and the run would write a
    phantom tick at a new timestamp for a price that never moved. Seeded, the
    series continues exactly as a full replay would -- which also makes a small
    overlap at the cursor harmless.

    UNLIKE `snapshot.build`, a payload that will not parse is skipped and
    counted rather than raised. The difference is deliberate and is about what
    the input is. `snapshot.build` reads the CURRENT payload, where a parse
    failure means the pipeline is broken right now and must stop. This replays
    WEEKS of payloads, where a body written under an older response shape is an
    ordinary fact about history -- and failing the whole backfill on one of
    them would mean the chart can never be built at all. The count keeps that
    from becoming an excuse: if it is not near zero, the crosswalk has a
    problem and the number says so.
    """
    last: dict[tuple[str, str, str], float] = dict(seed or {})
    ticks: list[Tick] = []
    failed = 0

    for record in history:
        if record.bookmaker not in PARSER_REGISTRY:
            continue
        # A payload is ~300 KB of JSON: give way to the hot loop between them.
        yield_to_hot()

        try:
            markets = parse_bookmaker(record.bookmaker, json.loads(record.payload), mappings)
        except Exception:
            failed += 1
            continue

        for market in markets:
            # '18;2.5' -> '18'. The specifier is the LINE, and a market is the
            # same market at every line.
            if market.market_id.split(";")[0] not in wanted_markets:
                continue
            for outcome in market.outcomes:
                # A price below 1.00 is not a price. Decimal odds of 1.00
                # return the stake and nothing else, and 0.00 returns
                # nothing at all -- no book offers either as a bet. They are
                # how a book says "this outcome is not currently available",
                # and one book in particular emits 0.00 for a suspended
                # outcome rather than omitting it (38 of 7,765 ticks).
                #
                # Charting them draws a price collapsing to zero and
                # recovering, which is a suspension being rendered as a
                # market move. See FINDINGS 1b: the same values reach the
                # arbitrage engine, where 1/0 is inf and the market is
                # silently never flagged.
                if outcome.odds <= 1.0:
                    continue
                key = (record.bookmaker, market.market_id, outcome.id)
                # `!=` rather than a tolerance: these are prices a book
                # published, not computed floats, so they compare exactly and
                # a 2.00 -> 2.01 move is a real move.
                if last.get(key) == outcome.odds:
                    continue
                last[key] = outcome.odds
                ticks.append(
                    Tick(
                        bookmaker=record.bookmaker,
                        market_id=market.market_id,
                        outcome_id=outcome.id,
                        outcome_name=outcome.name,
                        odds=outcome.odds,
                        fire_time=record.fire_time,
                    )
                )

    return Extraction(ticks=ticks, failed_payloads=failed)
