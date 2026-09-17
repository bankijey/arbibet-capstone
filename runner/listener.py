"""Hear, instantly, which fixtures just received a bookmaker payload.

Markets bronze has a trigger (sql/bronze_notify.sql) that sends the fixture's
event_id on channel `bronze_payload` for every inserted payload. This thread
LISTENs, collects the ids into a pending set, and sets `wake` so the hot loop
starts at once.

It never decides anything. If the connection drops it says so (`connected`
turns False) and reconnects with backoff; the hot loop then falls back to
polling bronze every 15 seconds, so a dead listener costs latency, never
signals.

It must keep draining. Postgres queues notifications for a listening session
that does not read them, and a full queue (8 GB) would make NOTIFY fail -- the
trigger swallows that error, so the collectors would be fine, but it is still
the reason this loop does nothing except read.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

import psycopg

from runner.observe import Observer

log = logging.getLogger("runner.listener")

CHANNEL = "bronze_payload"


class BronzeListener(threading.Thread):
    def __init__(self, dsn: str, observer: Observer, stop: threading.Event) -> None:
        super().__init__(name="listener", daemon=True)
        self.dsn = dsn
        self.observer = observer
        self.stop = stop
        self.wake = threading.Event()
        self.connected = False
        self.last_notification: datetime | None = None
        self.received = 0
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    def drain(self) -> set[str]:
        """Take every event_id notified since the last drain."""
        with self._lock:
            ids, self._pending = self._pending, set()
            self.wake.clear()
            return ids

    def run(self) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            try:
                with psycopg.connect(self.dsn, autocommit=True) as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    self.connected = True
                    backoff = 1.0
                    log.info("listening on %s", CHANNEL)
                    self.observer.beat("listener", "listening")
                    while not self.stop.is_set():
                        for note in conn.notifies(timeout=30, stop_after=5000):
                            with self._lock:
                                self._pending.add(note.payload)
                            self.received += 1
                            self.last_notification = datetime.now(UTC)
                            self.wake.set()
                        self.observer.beat(
                            "listener",
                            "listening",
                            received=self.received,
                            last_notification=self.last_notification,
                        )
            except Exception as err:
                self.connected = False
                log.warning("listener disconnected (%s); retrying in %.0fs", err, backoff)
                self.observer.beat("listener", "error", error=str(err)[:300])
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        self.connected = False
