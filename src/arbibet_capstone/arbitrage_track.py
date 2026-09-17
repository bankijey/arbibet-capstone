"""The arbitrage a market offered over time, rebuilt by the detector itself.

A surebet card shows one instant: the moment the consumer caught it. A reader
wants to know whether it is still there -- or, for a played match, where it
stood at kick-off -- and what it did in between, including below 1.0.

WHY NOT FROM THE TICK STORE
---------------------------
The obvious rebuild is from `fact_odds_tick`: carry each book's last price
forward, take the best per outcome, `1 / sum(1 / best)`. It was built, and it
reproduced only 14 of 25 recorded detections within 0.005 -- the other 11 by up
to +0.24, rebuilt 1.30 against a detected 1.06. The tick store records CHANGES,
so it cannot see a book withdrawing a price, nor tell a price standing unchanged
for two hours from one abandoned two hours ago. Both look identical, and a
dashboard figure of "arbitrage now: 1.30" would have been exactly the kind of
plausible wrong number this project exists to catch.

So this replays bronze the way the live pipeline sees it: after every payload,
each book's LATEST payload with its fire_time, through `snapshot`-shaped input
and the very same `signals.arbitrage_rows`. The spread, the single-book rule and
the arithmetic are the detector's, so a detection and the track cannot disagree.

WHAT A POINT IS
---------------
Emitted only when the market's state changes: its arbitrage, whether its legs
are within the freshness bar, or whether a cross-book price exists at all
(`arbitrage` NULL -- one book pricing every outcome, or the market gone). A
price series that repeats itself thousands of times is not a series.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, NamedTuple

from arbibet_capstone.bronze import HistoricPayload
from arbibet_capstone.crosswalk.parsers import PARSER_REGISTRY, parse_bookmaker
from arbibet_capstone.fixtures import Fixture
from arbibet_capstone.priority import yield_to_hot
from arbibet_capstone.signals import MAX_LEG_SPREAD_SECONDS, arbitrage_rows
from arbibet_capstone.snapshot import BookQuote, Snapshot

log = logging.getLogger(__name__)

# Effectively unbounded. The track records stale sets too -- with their spread --
# so a chart can show a surebet going stale rather than silently disappearing.
_NO_SPREAD_LIMIT = 10**9


class TrackPoint(NamedTuple):
    market_id: str
    observed_at: datetime                 # the payload that changed the picture
    arbitrage: float | None               # None: no cross-book price at this moment
    leg_spread_seconds: int | None
    newest_leg_fire_time: datetime | None
    legs: list[dict[str, Any]] | None


class _State(NamedTuple):
    arbitrage: float | None
    fresh: bool


class Track(NamedTuple):
    points: list[TrackPoint]
    failed_payloads: int
    last_fire_time: datetime | None


def replay(
    fixture: Fixture,
    history: Iterable[HistoricPayload],
    tracked: set[str],
    mappings: Any,
    seed_payloads: Iterable[HistoricPayload] = (),
    seed_state: Mapping[str, tuple[float | None, bool]] | None = None,
    max_leg_spread_seconds: int = MAX_LEG_SPREAD_SECONDS,
) -> Track:
    """Every state change of each `tracked` market (full ids, e.g. '18;2.5').

    `history` is replayed in fire_time order across books. `seed_payloads` is
    each book's latest payload BEFORE the history starts -- without it an
    incremental run would compute its first snapshots from whichever books
    happened to publish after the cursor, and report arbitrages over a partial
    market. `seed_state` is the last stored state per market, so resuming does
    not re-emit a point for a state that has not changed.
    """
    bases = {m.split(";")[0] for m in tracked}
    books: dict[str, BookQuote] = {}
    failed = 0

    def absorb(record: HistoricPayload) -> bool:
        nonlocal failed
        if record.bookmaker not in PARSER_REGISTRY:
            return False
        try:
            markets = parse_bookmaker(record.bookmaker, json.loads(record.payload), mappings)
        except Exception:
            # Weeks of history include bodies written under older response
            # shapes; counted, not raised -- the same rule as ticks.py.
            failed += 1
            return False
        wanted = [m for m in markets if m.market_id.split(";")[0] in bases]
        if wanted:
            books[record.bookmaker] = BookQuote(wanted, record.fire_time)
        else:
            # The book no longer prices any tracked market. Keeping its old
            # quote would be the tick store's mistake again.
            books.pop(record.bookmaker, None)
        return True

    for record in sorted(seed_payloads, key=lambda r: r.fire_time):
        absorb(record)

    state: dict[str, _State] = {
        m: _State(a, f) for m, (a, f) in (seed_state or {}).items()
    }
    points: list[TrackPoint] = []
    last_fire: datetime | None = None

    for record in sorted(history, key=lambda r: r.fire_time):
        yield_to_hot()
        if not absorb(record):
            continue
        last_fire = record.fire_time
        snapshot = Snapshot(fixture=fixture, books=dict(books))
        rows = {
            r["market_id"]: r
            for r in arbitrage_rows(
                snapshot, threshold=0.0, max_leg_spread_seconds=_NO_SPREAD_LIMIT
            )
            if r["market_id"] in tracked
        }
        for market_id in tracked:
            row = rows.get(market_id)
            if row is None:
                now = _State(None, False)
            else:
                now = _State(
                    round(float(row["arbitrage"]), 6),
                    row["leg_spread_seconds"] <= max_leg_spread_seconds,
                )
            if state.get(market_id) == now:
                continue
            if market_id not in state and row is None:
                # Nothing to say yet: the market has never had a cross-book
                # price in this replay, and "still unavailable" is not news.
                continue
            state[market_id] = now
            points.append(
                TrackPoint(
                    market_id=market_id,
                    observed_at=record.fire_time,
                    arbitrage=now.arbitrage,
                    leg_spread_seconds=None if row is None else row["leg_spread_seconds"],
                    newest_leg_fire_time=None if row is None else row["newest_leg_fire_time"],
                    legs=None if row is None else row["legs"],
                )
            )
    return Track(points=points, failed_payloads=failed, last_fire_time=last_fire)
