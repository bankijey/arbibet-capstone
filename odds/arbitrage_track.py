"""Track every market that has ever carried a surebet, before and after.

    bronze payload history -> each book's latest payload -> the detector -> track

Run:
    python odds/arbitrage_track.py

Scope: every (fixture, market) with at least one FRESH surebet in
`fact_arbitrage_signal` -- above 1.0, legs within five minutes. Played fixtures
included, replayed up to kick-off: in-play prices are the artefact FINDINGS 12b
and 13g describe, and "where it stood at kick-off" is the closing answer.

INCREMENTAL. A cursor per (fixture, market) in `pipeline_cursor` records the
newest payload already replayed. A resumed replay is seeded twice, and both are
needed: with each book's payload AS OF the cursor, or its first snapshots would
be built from whichever books happened to publish afterwards; and with the last
stored state per market, or it would re-emit a point for a state that has not
changed. A market newly added to a fixture's tracked set replays that fixture
from the start. Re-emitted points MERGE onto themselves.

See `arbibet_capstone.arbitrage_track` for why this replays the detector rather
than rebuilding from the tick store.
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from arbibet_capstone import bronze, cursor
from arbibet_capstone.arbitrage_track import replay
from arbibet_capstone.crosswalk.mappings import market_mappings
from arbibet_capstone.env import load as load_env
from arbibet_capstone.fixtures import Fixture
from arbibet_capstone.signals import MAX_LEG_SPREAD_SECONDS
from arbibet_capstone.warehouse import connect, merge_bulk

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# The vendored engine logs timings on every call, and a replay calls it once
# per payload.
logging.getLogger("arbibet_capstone.crosswalk.arbitrage").setLevel(logging.WARNING)
log = logging.getLogger("arbitrage_track")

TABLE = "fact_arbitrage_track"
CURSOR_SCOPE = "arbitrage_track"

_TRACKED = f"""
    SELECT s.event_id, s.market_id, f.kickoff_at
    FROM CORE.fact_arbitrage_signal s
    JOIN CORE.dim_fixture f ON f.event_id = s.event_id
    WHERE s.arbitrage > 1 AND s.leg_spread_seconds <= {MAX_LEG_SPREAD_SECONDS}
    GROUP BY 1, 2, 3
"""

_STORED = f"""
    SELECT event_id, market_id, observed_at, arbitrage, leg_spread_seconds
    FROM CORE.{TABLE}
    ORDER BY observed_at
"""

# Resume this far BEFORE the cursor. A payload can reach bronze after a run has
# passed its fire_time -- one stamped 20:32:52 was written after a 20:33 run had
# started -- and books stamp fire_time on their own clocks. Without overlap that
# payload is behind the cursor forever. Replaying the overlap is harmless: the
# seed state is taken as of the overlap's start, and re-emitted points MERGE
# onto themselves.
OVERLAP = timedelta(minutes=15)


def _key(event_id: str, market_id: str) -> str:
    return f"{event_id}|{market_id}"


def _state_as_of(
    points: list[tuple[datetime, float | None, int | None]], moment: datetime
) -> tuple[float | None, bool] | None:
    """The stored (arbitrage, fresh) a market was in at `moment`, if any."""
    before = [p for p in points if p[0] <= moment]
    if not before:
        return None
    _, arbitrage, spread = before[-1]
    return (
        None if arbitrage is None else round(float(arbitrage), 6),
        arbitrage is not None and spread is not None and int(spread) <= MAX_LEG_SPREAD_SECONDS,
    )


def main() -> int:
    with connect() as warehouse, warehouse.cursor() as cur:
        cur.execute(_TRACKED)
        tracked: dict[str, set[str]] = defaultdict(set)
        kickoff: dict[str, datetime] = {}
        for event_id, market_id, kicked_off in cur.fetchall():
            tracked[str(event_id)].add(str(market_id))
            kickoff[str(event_id)] = kicked_off
        cur.execute(_STORED)
        stored: dict[str, list[tuple[datetime, float | None, int | None]]] = defaultdict(list)
        for e, m, t, a, spread in cur.fetchall():
            stored[_key(str(e), str(m))].append((t, a, spread))
        positions = cursor.read(warehouse, CURSOR_SCOPE)

    log.info("fixtures=%d markets=%d", len(tracked), sum(len(m) for m in tracked.values()))
    mappings = market_mappings()
    rows: dict[tuple[str, str, datetime], dict[str, Any]] = {}
    advanced: dict[str, datetime] = {}
    failed = 0

    with bronze.connect() as source:
        for event_id, markets in sorted(tracked.items()):
            known = [positions.get(_key(event_id, m)) for m in markets]
            resume = (
                None if any(p is None for p in known) else min(p for p in known if p) - OVERLAP
            )
            fixture = Fixture(
                UUID(event_id), kickoff[event_id], None, None, None, None, None, None, None
            )
            history = bronze.payload_history(
                source, UUID(event_id), since=resume, until=kickoff[event_id]
            )
            if not history:
                continue
            track = replay(
                fixture,
                history,
                markets,
                mappings,
                seed_payloads=(
                    bronze.payloads_as_of(source, UUID(event_id), resume) if resume else ()
                ),
                seed_state=(
                    {
                        m: state
                        for m in markets
                        if (state := _state_as_of(stored[_key(event_id, m)], resume)) is not None
                    }
                    if resume
                    else None
                ),
            )
            failed += track.failed_payloads
            for point in track.points:
                # Keyed on the natural key, so a replay that emits the same
                # moment twice keeps one row rather than failing the MERGE.
                rows[(event_id, point.market_id, point.observed_at)] = {
                    "event_id": event_id,
                    "market_id": point.market_id,
                    "observed_at": point.observed_at,
                    "arbitrage": point.arbitrage,
                    "leg_spread_seconds": point.leg_spread_seconds,
                    "newest_leg_fire_time": point.newest_leg_fire_time,
                }
            if track.last_fire_time is not None:
                for m in markets:
                    advanced[_key(event_id, m)] = track.last_fire_time
            log.info(
                "%s payloads=%d points=%d%s",
                event_id, len(history), len(track.points), "" if resume else " (full replay)",
            )

    with connect() as warehouse:
        # Staged in one round trip. The legs are not stored: they live on the
        # detection in fact_arbitrage_signal, and a VARIANT column forced
        # row-at-a-time MERGEs -- about a second per point, 664 points in ten
        # minutes -- on a task that must finish inside a 30-minute schedule.
        written = (
            merge_bulk(
                warehouse,
                table=TABLE,
                rows=list(rows.values()),
                key=["event_id", "market_id", "observed_at"],
            )
            if rows
            else 0
        )
        # Only after the points are safely written: a cursor that ran ahead of
        # its data would skip that history forever.
        cursor.write(warehouse, CURSOR_SCOPE, advanced)
    log.info("points=%d cursors=%d failed_payloads=%d", written, len(advanced), failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
