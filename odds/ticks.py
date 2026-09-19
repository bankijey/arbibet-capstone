"""Backfill `fact_odds_tick` for the markets that produced signals.

    bronze payload history -> crosswalk -> price changes -> the warehouse

Run:
    python odds/ticks.py

Scope: every (fixture, market) that appears in `fact_arbitrage_signal` or
`fact_ev_signal`. That bound is the whole reason this is cheap. Extracting
every market in every payload would be a few hundred thousand rows, almost
none of which anything looks at; extracting the markets a signal was actually
raised on is a few thousand, and they are the ones a reader wants to interrogate.

INCREMENTAL by default. Each fixture resumes from the newest tick already
stored for it, so a daily run parses the few hundred payloads that arrived
since yesterday rather than every payload ever written. The first version
replayed everything every time: 9,732 payloads and 48 minutes to derive about
4,800 new rows, with ~95% of the work re-deriving rows already in the table.

The cursor alone would be WRONG, though -- see `_SEEDS`. A run that starts
mid-history with no memory of the last price treats the first payload after the
cursor as a change, so the state is seeded from what is already stored and the
series continues exactly as a full replay would.

Bounding knobs, matching the rest of the pipeline:
    TICKS_LIMIT        maximum fixtures to process (default: all of them)
    TICKS_FULL_REPLAY  =1 to rebuild from the start of bronze, after a parser
                       fix has invalidated everything already derived

Safe to re-run either way: the MERGE key is the natural key, so re-processing a
fixture rewrites the same rows rather than duplicating them.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from uuid import UUID

from arbibet_capstone import bronze
from arbibet_capstone.crosswalk.mappings import market_mappings
from arbibet_capstone.deep_dives import popular_fixtures_sql
from arbibet_capstone.env import load as load_env
from arbibet_capstone.ticks import price_changes
from arbibet_capstone.verify import mismatched_books
from arbibet_capstone.warehouse import bookmaker_ids, connect, merge_bulk

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ticks")

# Both signal tables, because both are charted. UNION rather than UNION ALL:
# an arbitrage and an EV signal on the same market is one series, not two.
#
# The BASE market id, without the specifier: pulling every line of a market
# rather than only the one that fired lets the chart offer 2.5 and 3.5 side by
# side, at no meaningful extra cost.
_SIGNAL_MARKETS = """
    SELECT event_id, split_part(market_id, ';', 1) AS market_base FROM CORE.fact_arbitrage_signal
    UNION
    SELECT event_id, split_part(market_id, ';', 1) AS market_base FROM CORE.fact_ev_signal
"""

# What a deep dive charts. Base betradar ids: 1x2, double chance, over/under,
# both teams to score. Deliberately a short list -- every market in every
# payload would be a few hundred thousand rows for five fixtures, and these
# four are the ones slips are actually built from.
_DEEP_DIVE_MARKETS = {"1", "10", "18", "29"}

# Where each fixture's replay resumes: the newest price already stored for it.
_CURSORS = """
    SELECT event_id, max(fire_time) FROM CORE.fact_odds_tick GROUP BY event_id
"""

# The last price known for every (fixture, book, market, outcome).
#
# Without this an incremental run is WRONG, not merely partial: `price_changes`
# emits a tick when a price differs from the previous one it saw, so a run that
# starts mid-history with no memory treats the first payload after the cursor
# as a change and writes a phantom tick for a price that never moved. Seeded,
# the series continues exactly as a full replay would -- and a small overlap at
# the cursor costs nothing.
_SEEDS = """
    SELECT t.event_id, b.bookmaker_name, t.market_id, t.outcome_id, t.odds
    FROM CORE.fact_odds_tick t
    JOIN CORE.dim_bookmaker b ON b.bookmaker_id = t.bookmaker_id
    QUALIFY row_number() OVER (
        PARTITION BY t.event_id, t.bookmaker_id, t.market_id, t.outcome_id
        ORDER BY t.fire_time DESC
    ) = 1
"""


def main() -> int:
    limit = int(os.environ.get("TICKS_LIMIT", "0"))
    # Set TICKS_FULL_REPLAY=1 to rebuild from the beginning of bronze -- after
    # a parser fix, when every stored tick was derived by the old code.
    full_replay = os.environ.get("TICKS_FULL_REPLAY") == "1"

    with connect() as warehouse, warehouse.cursor() as cur:
        wanted: dict[str, set[str]] = defaultdict(set)

        cur.execute(_SIGNAL_MARKETS)
        for event_id, market_base in cur.fetchall():
            wanted[str(event_id)].add(str(market_base))

        # Deep-dive fixtures have no signals, so nothing above would pull
        # their prices, and a deep dive with no price history is a page of
        # static facts.
        cur.execute(popular_fixtures_sql())
        for event_id, _upcoming in cur.fetchall():
            # `|=`, not `=`: a fixture can be both popular and signalled, and
            # the deep dive must not narrow what the signal chart already has.
            wanted[str(event_id)] |= _DEEP_DIVE_MARKETS

        books = bookmaker_ids(warehouse)
        # Books found pricing a different match under a fixture's id
        # (core.fixture_check): their prices are not this fixture's history.
        excluded = mismatched_books(warehouse)

        cursors: dict[str, datetime] = {}
        seeds: dict[str, dict[tuple[str, str, str], float]] = defaultdict(dict)
        if not full_replay:
            cur.execute(_CURSORS)
            cursors = {str(e): t for e, t in cur.fetchall()}
            cur.execute(_SEEDS)
            for event_id, book, market_id, outcome_id, odds in cur.fetchall():
                seeds[str(event_id)][(str(book), str(market_id), str(outcome_id))] = float(odds)

    fixtures = sorted(wanted)
    if limit:
        fixtures = fixtures[:limit]
    log.info("fixtures=%d markets=%d", len(fixtures), sum(len(m) for m in wanted.values()))

    mappings = market_mappings()
    rows: list[dict[str, object]] = []
    skipped = 0
    # Where the time goes. Transfer dominated before the runner joined the
    # markets database's Docker network: 0.8 MB/s through host.docker.internal
    # against ~30 MB/s direct, for payloads of ~300 KB each.
    fetch_seconds = parse_seconds = 0.0
    payloads = megabytes = 0

    with bronze.connect() as source:
        for event_id in fixtures:
            started = time.monotonic()
            history = bronze.payload_history(source, UUID(event_id), cursors.get(event_id))
            fetch_seconds += time.monotonic() - started
            if event_id in excluded:
                history = [
                    h
                    for h in history
                    if "*" not in excluded[event_id] and h.bookmaker not in excluded[event_id]
                ]
            payloads += len(history)
            megabytes += sum(len(h.payload) for h in history)
            if not history:
                # Two very different reasons, and only one is worth a warning.
                # An incremental run reaching a fixture whose prices have not
                # moved since last time is the normal case -- the whole point.
                # A fixture we have never seen and bronze has no rows for has
                # been pruned, and that is worth saying out loud.
                if event_id not in cursors:
                    log.warning("no bronze history for %s (pruned?)", event_id)
                continue

            started = time.monotonic()
            extraction = price_changes(history, wanted[event_id], mappings, seeds.get(event_id))
            parse_seconds += time.monotonic() - started
            skipped += extraction.failed_payloads

            for tick in extraction.ticks:
                if tick.bookmaker not in books:
                    continue
                rows.append(
                    {
                        "event_id": event_id,
                        "market_id": tick.market_id,
                        "outcome_id": tick.outcome_id,
                        "outcome_name": tick.outcome_name,
                        "bookmaker_id": books[tick.bookmaker],
                        "odds": tick.odds,
                        "fire_time": tick.fire_time,
                    }
                )
            log.info(
                "%s payloads=%d ticks=%d%s",
                event_id,
                len(history),
                len(rows),
                "" if full_replay or event_id in cursors else " (first pass)",
            )

    log.info(
        "payloads=%d mb=%.0f fetch=%.0fs parse=%.0fs",
        payloads,
        megabytes / 1e6,
        fetch_seconds,
        parse_seconds,
    )
    if not rows:
        log.warning("no ticks extracted")
        return 0

    with connect() as warehouse:
        written = merge_bulk(
            warehouse,
            table="fact_odds_tick",
            rows=rows,
            key=["event_id", "market_id", "outcome_id", "bookmaker_id", "fire_time"],
        )
    log.info(
        "ticks=%d skipped_payloads=%d mode=%s",
        written,
        skipped,
        "full replay" if full_replay else "incremental",
    )
    return written


if __name__ == "__main__":
    sys.exit(0 if main() >= 0 else 1)
