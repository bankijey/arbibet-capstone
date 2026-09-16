"""Recompute arbitrage and EV the moment new markets bronze data lands.

WHY THIS EXISTS
---------------
`fact_arbitrage_signal` and `fact_ev_signal` are written only by
`consumers/arb.py` and `consumers/ev.py`, which are one-shot batch passes, and
those run only inside the daily Airflow DAG. Between runs nothing recomputes, so
the dashboard shows yesterday's signals. The legacy platform did not work that
way: a local watcher tailed markets bronze, and the instant new prices landed
for a match it recomputed and pushed any opportunity onward. This is that
watcher.

WHAT IT DOES
------------
Every `WATCH_POLL_SECONDS` it asks markets bronze for the newest write_time per
UPCOMING fixture. For any fixture whose newest payload is newer than the one it
last processed, it rebuilds the snapshot through the SAME crosswalk the producer
uses and runs the SAME `arbitrage_rows` / `ev_rows` the consumers run, then
MERGEs any signal to Snowflake on `signal_key`. Because the key is identical,
this path and the batch path converge rather than duplicate -- run both, and a
signal found by either is the same row.

GUARD RAILS -- why it is safe to run continuously
-------------------------------------------------
* PRE-KICKOFF ONLY. Arbitrage and EV only matter while the bet can still be
  placed, and an in-play match rewrites its prices constantly -- the payload
  storm that spikes the line-movement chart. Fixtures that have kicked off are
  dropped from the watch set, which is both correct and what bounds the work.
* SNOWFLAKE IS TOUCHED ONLY TO WRITE. The warehouse auto-suspends after five
  idle minutes and bills compute only on query. A recompute that finds no
  opportunity never runs a statement against it, so a quiet hour costs nothing;
  opportunities are rare, so writes are rare.
* BRONZE IS POLLED BY THE INDEXED KEY. `bronze.latest_write_times` filters
  `event_id = ANY(...)` over the upcoming set, which the
  (event_id, bookmaker, write_time) index serves. A `write_time > cursor` scan
  of the 6.9M-row table every 20s is the shape the bronze docstring warns
  against, and this avoids it.

Run:
    python watch/signals.py
Stop with Ctrl-C. In a real deployment the odds path runs on a cadence like
this and only the history path is daily (see the DAG's module docstring).
"""

from __future__ import annotations

import logging
import os
import time
import warnings
from datetime import UTC, datetime, timedelta
from uuid import UUID

from arbibet_capstone import bronze, fixtures
from arbibet_capstone.crosswalk.mappings import market_mappings
from arbibet_capstone.env import load as load_env
from arbibet_capstone.fixtures import Fixture
from arbibet_capstone.signals import arbitrage_rows, ev_rows
from arbibet_capstone.snapshot import build
from arbibet_capstone.warehouse import bookmaker_ids, merge
from arbibet_capstone.warehouse import connect as warehouse_connect

load_env()

# The vendored engine concatenates empty frames on nearly every recompute and
# logs a timing line per stage; both are noise in a loop. Same suppression the
# consumers use.
warnings.filterwarnings(
    "ignore", category=FutureWarning, module="arbibet_capstone.crosswalk"
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("arbibet_capstone.crosswalk.arbitrage").setLevel(logging.WARNING)
log = logging.getLogger("watch.signals")


def _upcoming(sources: object, until_hours: float) -> dict[UUID, Fixture]:
    """The fixtures worth watching: priced soon, and not yet kicked off.

    HOURS, not days, and that bound is load-bearing. Markets bronze only
    actively reprices fixtures close to kickoff, so a wide window adds hundreds
    of fixtures that never change while making the poll expensive: `= ANY(...)`
    over a 1,300-element array turns `latest_write_times` into a seq scan of a
    6.9M-row table -- forty seconds a tick, observed. A few hours keeps the
    array to dozens, which the (event_id, ...) index serves in well under a
    second, and captures essentially all the pre-kickoff price movement there
    is. Fixtures further out are the daily DAG's job.

    `since` reaches slightly into the past only because `upcoming` filters on a
    window; the `kickoff > now` test is what actually enforces pre-kickoff, so
    an in-play or finished fixture is never watched.
    """
    now = datetime.now(UTC)
    window = fixtures.upcoming(
        sources,  # type: ignore[arg-type]
        since=now - timedelta(hours=2),
        until=now + timedelta(hours=until_hours),
    )
    return {f.event_id: f for f in window if f.kickoff > now}


def _recompute(
    bronze_conn: object,
    fixture: Fixture,
    mappings: object,
    arb_threshold: float,
    ev_min: float,
    ev_min_probability: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """This fixture's current arbitrage and EV rows, from its latest prices."""
    payloads = bronze.latest_payloads(bronze_conn, fixture.event_id)  # type: ignore[arg-type]
    snapshot = build(fixture, payloads)
    arb = arbitrage_rows(snapshot, threshold=arb_threshold)
    ev = ev_rows(snapshot, min_ev=ev_min, min_probability=ev_min_probability)
    return arb, ev


def _write(
    warehouse: object,
    books: dict[str, int],
    arb: list[dict[str, object]],
    ev: list[dict[str, object]],
) -> int:
    """MERGE the rows. Only ever called when there is something to write."""
    written = 0
    consumed_at = datetime.now(UTC)
    if arb:
        written += merge(
            warehouse,  # type: ignore[arg-type]
            table="fact_arbitrage_signal",
            rows=[{**r, "consumed_at": consumed_at} for r in arb],
            key=["signal_key"],
            json_columns={"legs"},
        )
    if ev:
        rows = []
        for row in ev:
            book = row.pop("bookmaker")
            if book not in books:
                # A book priced it but dim_bookmaker does not know it. Same
                # handling as the EV consumer: say which, drop the row, go on.
                log.warning("no dim_bookmaker row for %s", book)
                continue
            rows.append({"bookmaker_id": books[book], **row, "consumed_at": consumed_at})
        if rows:
            written += merge(
                warehouse,  # type: ignore[arg-type]
                table="fact_ev_signal",
                rows=rows,
                key=["signal_key"],
            )
    return written


def main() -> int:
    poll_seconds = float(os.environ.get("WATCH_POLL_SECONDS", "20"))
    refresh_seconds = float(os.environ.get("WATCH_REFRESH_SECONDS", "300"))
    until_hours = float(os.environ.get("WATCH_UNTIL_HOURS", "6"))
    arb_threshold = float(os.environ.get("ARB_RECORD_THRESHOLD", "0.98"))
    ev_min = float(os.environ.get("EV_MIN", "0.01"))
    ev_min_probability = float(os.environ.get("EV_MIN_PROBABILITY", "0.5"))

    mappings = market_mappings()
    sources = fixtures.connect()
    bronze_conn = bronze.connect()
    warehouse = warehouse_connect()
    # Read once. dim_bookmaker is five rows that essentially never change, and
    # this is the one Snowflake query that runs whether or not an opportunity
    # is found -- everything else waits for a signal.
    books = bookmaker_ids(warehouse)

    watched: dict[UUID, Fixture] = {}
    seen: dict[UUID, datetime] = {}
    last_refresh = 0.0
    primed = False

    log.info(
        "watching every %.0fs, pre-kickoff fixtures only, arb>=%.2f",
        poll_seconds, arb_threshold,
    )
    try:
        while True:
            if time.monotonic() - last_refresh > refresh_seconds:
                watched = _upcoming(sources, until_hours)
                # A fixture that has kicked off since the last refresh drops out
                # of `watched`; forget its cursor too, so the dict cannot grow
                # without bound over a long run.
                seen = {e: w for e, w in seen.items() if e in watched}
                last_refresh = time.monotonic()
                log.info("watching %d upcoming fixtures", len(watched))

            latest = bronze.latest_write_times(bronze_conn, list(watched))

            # PRIME, once. With `seen` empty every priced fixture looks changed,
            # and on a 3-day window that is ~1,300 recomputes in the first tick
            # -- recomputing a backlog the daily DAG has already written. The
            # watcher's job is prices that land AFTER it starts, so the first
            # tick just records the baseline and reacts to the next change.
            if not primed:
                seen = dict(latest)
                primed = True
                log.info("primed %d priced fixtures; watching for new prices", len(latest))
                time.sleep(poll_seconds)
                continue

            changed = [e for e, wt in latest.items() if seen.get(e) != wt]

            written = 0
            for event_id in changed:
                try:
                    arb, ev = _recompute(
                        bronze_conn, watched[event_id], mappings,
                        arb_threshold, ev_min, ev_min_probability,
                    )
                except Exception:
                    # One fixture's bad payload is not the loop's problem, and
                    # NOT advancing its cursor means the next poll retries it.
                    log.error("recompute failed for %s", event_id, exc_info=True)
                    continue
                if arb or ev:
                    written += _write(warehouse, books, arb, ev)
                seen[event_id] = latest[event_id]

            if written:
                log.info(
                    "wrote %d signal rows from %d changed fixtures",
                    written, len(changed),
                )
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        log.info("watcher stopped")
        return 0
    finally:
        sources.close()
        bronze_conn.close()
        warehouse.close()


if __name__ == "__main__":
    raise SystemExit(main())
