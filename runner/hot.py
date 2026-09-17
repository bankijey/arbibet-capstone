"""The hot loop: surebets and positive EV within seconds of a new price.

Grown from `watch/signals.py`. For every fixture that has not kicked off, the
moment bronze stores a new payload for it, rebuild that fixture's snapshot
through the same crosswalk and run the same `arbitrage_rows` / `ev_rows` the
batch path used, then MERGE on `signal_key` -- so this path and any batch
recompute converge on the same rows rather than duplicating them.

HOW IT WAKES
------------
* NOTIFY (instant): the listener hands over the event_ids bronze just wrote.
  A short debounce first, so one collector cycle's burst of payloads for a
  fixture (one per book) becomes one recompute, not five.
* POLL (fallback, every 15 s): `bronze.latest_write_times` for the watched
  fixtures, by the indexed key. Runs whether or not the listener is up: it is
  cheap, and it is what catches anything a dropped connection missed.

WHAT IT RECORDS
---------------
One `ops.job_run` summary row a minute (`hot.signals`) rather than one per
wake -- a busy afternoon wakes this loop thousands of times. The detail holds
wakes, fixtures recomputed, rows written, failures, and LATENCY: seconds from
bronze writing the newest payload to the signal being stored. That number is
the loop's whole purpose, so it is measured rather than assumed.
"""

from __future__ import annotations

import logging
import os
import statistics
import threading
import time
import warnings
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from arbibet_capstone import bronze, fixtures
from arbibet_capstone.fixtures import Fixture
from arbibet_capstone.signals import arbitrage_rows, ev_rows
from arbibet_capstone.snapshot import build
from arbibet_capstone.warehouse import Warehouse, bookmaker_ids, merge_bulk
from runner.listener import BronzeListener
from runner.observe import Observer

warnings.filterwarnings("ignore", category=FutureWarning, module="arbibet_capstone.crosswalk")
logging.getLogger("arbibet_capstone.crosswalk.arbitrage").setLevel(logging.WARNING)
log = logging.getLogger("runner.hot")

POLL_SECONDS = float(os.environ.get("HOT_POLL_SECONDS", "15"))
DEBOUNCE_SECONDS = float(os.environ.get("HOT_DEBOUNCE_SECONDS", "1.5"))
REFRESH_SECONDS = float(os.environ.get("HOT_REFRESH_SECONDS", "300"))
# Two horizons. NOTIFY costs nothing to wait on, so notified fixtures are
# acted on up to three days out -- most payloads bronze writes are for fixtures
# further than six hours away (measured: 1 of 35 notified fixtures in a 30 s
# sample was inside six hours). The fallback POLL stays at six hours, where the
# `= ANY(...)` over the watched ids is served by the index; over three days
# (~1,100 fixtures) it turns into a scan of the 7M-row table.
WATCH_HOURS = float(os.environ.get("HOT_WATCH_HOURS", "72"))
POLL_HOURS = float(os.environ.get("HOT_POLL_HOURS", "6"))
ARB_THRESHOLD = float(os.environ.get("ARB_RECORD_THRESHOLD", "0.98"))
EV_MIN = float(os.environ.get("EV_MIN", "0.01"))
EV_MIN_PROBABILITY = float(os.environ.get("EV_MIN_PROBABILITY", "0.5"))
SUMMARY_SECONDS = 60


class HotLoop(threading.Thread):
    def __init__(
        self,
        warehouse: Warehouse,
        observer: Observer,
        listener: BronzeListener,
        stop: threading.Event,
        on_signals: Any = None,
    ) -> None:
        super().__init__(name="hot", daemon=True)
        self.warehouse = warehouse
        self.observer = observer
        self.listener = listener
        self.stop = stop
        # Called with the number of rows written, so serving can be refreshed.
        self.on_signals = on_signals
        self.watched: dict[UUID, Fixture] = {}
        self.seen: dict[UUID, datetime] = {}
        self._stats = self._fresh_stats()

    @staticmethod
    def _fresh_stats() -> dict[str, Any]:
        return {
            "started_at": datetime.now(UTC),
            "wakes_notify": 0,
            "wakes_poll": 0,
            "fixtures": 0,
            "arbitrage_rows": 0,
            "ev_rows": 0,
            "failures": 0,
            "notified_ignored": 0,
            "latencies": [],
        }

    # --- work ------------------------------------------------------------------

    def _refresh(self, sources: Any) -> None:
        now = datetime.now(UTC)
        window = fixtures.upcoming(
            sources, since=now - timedelta(hours=2), until=now + timedelta(hours=WATCH_HOURS)
        )
        self.watched = {f.event_id: f for f in window if f.kickoff > now}
        self.seen = {e: w for e, w in self.seen.items() if e in self.watched}

    def _recompute(
        self, bronze_conn: Any, books: dict[str, int], event_ids: list[UUID], notified: bool
    ) -> int:
        written = 0
        latest = bronze.latest_write_times(bronze_conn, event_ids)
        for event_id in event_ids:
            fixture = self.watched.get(event_id)
            if fixture is None or fixture.kickoff <= datetime.now(UTC):
                continue
            try:
                payloads = bronze.latest_payloads(bronze_conn, event_id)
                snapshot = build(fixture, payloads)
                arb = arbitrage_rows(snapshot, threshold=ARB_THRESHOLD)
                ev = ev_rows(snapshot, min_ev=EV_MIN, min_probability=EV_MIN_PROBABILITY)
                written += self._write(books, arb, ev)
            except Exception:
                # One fixture's bad payload is not the loop's problem; not
                # advancing `seen` means the next poll retries it.
                self._stats["failures"] += 1
                log.error("recompute failed for %s", event_id, exc_info=True)
                continue
            self._stats["fixtures"] += 1
            if event_id in latest:
                self.seen[event_id] = latest[event_id]
                # Latency only for notified recomputes: a poll after a restart
                # would count however long the runner was down.
                if notified:
                    self._stats["latencies"].append(
                        (datetime.now(UTC) - latest[event_id]).total_seconds()
                    )
        return written

    def _write(self, books: dict[str, int], arb: list[dict], ev: list[dict]) -> int:
        consumed_at = datetime.now(UTC)
        written = 0
        if arb:
            written += merge_bulk(
                self.warehouse,
                table="fact_arbitrage_signal",
                rows=[{**r, "consumed_at": consumed_at} for r in arb],
                key=["signal_key"],
                json_columns={"legs"},
            )
            self._stats["arbitrage_rows"] += len(arb)
        rows = []
        for row in ev:
            row = dict(row)
            book = row.pop("bookmaker")
            if book not in books:
                log.warning("no dim_bookmaker row for %s", book)
                continue
            rows.append({"bookmaker_id": books[book], **row, "consumed_at": consumed_at})
        if rows:
            written += merge_bulk(
                self.warehouse, table="fact_ev_signal", rows=rows, key=["signal_key"]
            )
            self._stats["ev_rows"] += len(rows)
        return written

    def _summarise(self) -> None:
        stats, self._stats = self._stats, self._fresh_stats()
        latencies = stats.pop("latencies")
        started = stats.pop("started_at")
        detail = {
            **stats,
            "watched": len(self.watched),
            "listener_connected": self.listener.connected,
            "latency_median_s": round(statistics.median(latencies), 1) if latencies else None,
            "latency_max_s": round(max(latencies), 1) if latencies else None,
        }
        self.observer.record(
            "hot.signals",
            "hot",
            trigger="notify" if stats["wakes_notify"] else "poll",
            started_at=started,
            rows_written=stats["arbitrage_rows"] + stats["ev_rows"],
            detail=detail,
        )
        self.observer.beat("hot", "watching", **detail)

    # --- loop ------------------------------------------------------------------

    def run(self) -> None:
        sources = fixtures.connect()
        bronze_conn = bronze.connect()
        books = bookmaker_ids(self.warehouse)
        last_refresh = 0.0
        last_summary = time.monotonic()
        last_poll = 0.0
        primed = False
        log.info(
            "hot loop: notify for the next %.0fh, %.0fs poll for the next %.0fh",
            WATCH_HOURS, POLL_SECONDS, POLL_HOURS,
        )
        try:
            while not self.stop.is_set():
                try:
                    if time.monotonic() - last_refresh > REFRESH_SECONDS:
                        self._refresh(sources)
                        last_refresh = time.monotonic()
                        log.info("watching %d upcoming fixtures", len(self.watched))

                    notified = self.listener.wake.wait(timeout=POLL_SECONDS)
                    written = 0
                    if notified and not self.stop.is_set():
                        time.sleep(DEBOUNCE_SECONDS)
                        ids = self.listener.drain()
                        targets = [UUID(i) for i in ids if UUID(i) in self.watched]
                        self._stats["notified_ignored"] += len(ids) - len(targets)
                        if targets:
                            self._stats["wakes_notify"] += 1
                            written += self._recompute(bronze_conn, books, targets, notified=True)

                    if time.monotonic() - last_poll >= POLL_SECONDS:
                        last_poll = time.monotonic()
                        horizon = datetime.now(UTC) + timedelta(hours=POLL_HOURS)
                        polled = [e for e, f in self.watched.items() if f.kickoff <= horizon]
                        latest = bronze.latest_write_times(bronze_conn, polled)
                        if not primed:
                            # The first poll records the baseline: every priced
                            # fixture would otherwise look changed at start-up.
                            self.seen = dict(latest)
                            primed = True
                        else:
                            changed = [e for e, w in latest.items() if self.seen.get(e) != w]
                            if changed:
                                self._stats["wakes_poll"] += 1
                                written += self._recompute(
                                    bronze_conn, books, changed, notified=False
                                )

                    if written and self.on_signals:
                        self.on_signals(written)
                    if time.monotonic() - last_summary >= SUMMARY_SECONDS:
                        self._summarise()
                        last_summary = time.monotonic()
                except Exception as err:
                    log.error("hot loop iteration failed", exc_info=True)
                    self.observer.beat("hot", "error", error=str(err)[:300])
                    # Connections are the usual casualty; reopen them.
                    for conn in (sources, bronze_conn):
                        try:
                            conn.close()
                        except Exception:
                            pass
                    time.sleep(5)
                    sources = fixtures.connect()
                    bronze_conn = bronze.connect()
        finally:
            for conn in (sources, bronze_conn):
                try:
                    conn.close()
                except Exception:
                    pass
