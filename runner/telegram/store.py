"""The bot's reads and writes, all against Supabase, never DuckDB.

Commands read the same documents the dashboard reads (`serving.document`,
`serving.dive`) and write flags to the same table (`serving.leg_flag`), so the
bot and the dashboard always agree, and nothing a Telegram user does can touch
the warehouse or wait on a warm job holding it.

The bot's own state lives in a `bot` schema:

    bot.subscriber   one row per chat: subscribed?, alert kinds, EV threshold,
                     default stake, mute
    bot.alert        (chat, opportunity) -> the value last alerted, so a
                     restart does not re-send everything

Row-level security is enabled on both with NO policies: the dashboard's login
cannot read chat ids. The runner connects as the tables' owner.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import psycopg

from runner.serve import _connect, configured
from runner.telegram.model import Ledger, Subscriber

log = logging.getLogger("runner.telegram.store")
T = TypeVar("T")

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS bot;

CREATE TABLE IF NOT EXISTS bot.subscriber (
    chat_id      bigint PRIMARY KEY,
    username     text,
    active       boolean NOT NULL DEFAULT true,
    surebets     boolean NOT NULL DEFAULT true,
    ev           boolean NOT NULL DEFAULT true,
    ev_min       double precision NOT NULL DEFAULT 0.015,
    stake        double precision NOT NULL DEFAULT 100,
    muted_until  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot.alert (
    chat_id     bigint NOT NULL,
    key         text NOT NULL,
    value       double precision NOT NULL,
    alerted_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, key)
);

ALTER TABLE bot.subscriber ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot.alert ENABLE ROW LEVEL SECURITY;
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated', 'dashboard_reader'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA bot FROM %I', r);
            EXECUTE format('REVOKE ALL ON SCHEMA bot FROM %I', r);
        END IF;
    END LOOP;
END
$$;
"""

# Documents are re-read only when their generated_at moves; the check itself
# is cached this long.
VERSION_TTL = 15.0
ALERT_RETENTION = timedelta(days=7)

_SUBSCRIBER_COLUMNS = "chat_id, username, active, surebets, ev, ev_min, stake, muted_until"


class Store:
    def __init__(self) -> None:
        if not configured():
            raise RuntimeError("SUPABASE_DB_URL is not set")
        self._lock = threading.RLock()
        self._conn: psycopg.Connection | None = None
        self._versions: tuple[float, dict[str, str]] = (0.0, {})
        self._documents: dict[str, tuple[str, dict[str, Any]]] = {}
        self._flags: tuple[float, set[tuple[str, str, str]]] = (0.0, set())

    def _run(self, fn: Callable[[psycopg.Connection], T]) -> T:
        """One connection, one statement at a time, reconnecting once if it dropped."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = _connect()
                    return fn(self._conn)
                except psycopg.OperationalError:
                    if self._conn is not None:
                        self._conn.close()
                    self._conn = None
                    if attempt == 2:
                        raise
        raise AssertionError("unreachable")

    def ensure_schema(self) -> None:
        self._run(lambda c: c.execute(SCHEMA))

    # --- documents ----------------------------------------------------------------

    def document(self, name: str) -> dict[str, Any] | None:
        checked, versions = self._versions
        if time.monotonic() - checked > VERSION_TTL:
            rows = self._run(
                lambda c: c.execute("SELECT name, generated_at FROM serving.document").fetchall()
            )
            versions = {n: str(g) for n, g in rows}
            self._versions = (time.monotonic(), versions)
        version = versions.get(name)
        cached = self._documents.get(name)
        if cached and cached[0] == version:
            return cached[1]
        row = self._run(
            lambda c: c.execute(
                "SELECT body FROM serving.document WHERE name = %s", (name,)
            ).fetchone()
        )
        if row is None:
            return None
        self._documents[name] = (version or "", row[0])
        return row[0]

    def dive(self, event_id: str) -> dict[str, Any] | None:
        row = self._run(
            lambda c: c.execute(
                "SELECT body FROM serving.dive WHERE event_id = %s", (event_id,)
            ).fetchone()
        )
        return row[0] if row else None

    # --- flags --------------------------------------------------------------------

    def flags(self, max_age: float = 20.0) -> set[tuple[str, str, str]]:
        checked, flags = self._flags
        if time.monotonic() - checked > max_age:
            rows = self._run(
                lambda c: c.execute(
                    "SELECT event_id, market_id, bookmaker_name FROM serving.leg_flag WHERE active"
                ).fetchall()
            )
            flags = {(str(e), str(m), str(b)) for e, m, b in rows}
            self._flags = (time.monotonic(), flags)
        return flags

    def flag_rows(self) -> list[tuple[str, str, str, datetime]]:
        return self._run(
            lambda c: c.execute(
                "SELECT event_id, market_id, bookmaker_name, flagged_at FROM serving.leg_flag "
                "WHERE active ORDER BY flagged_at DESC"
            ).fetchall()
        )

    def set_flag(self, event_id: str, market_id: str, book: str, active: bool) -> None:
        self._run(
            lambda c: c.execute(
                """
                INSERT INTO serving.leg_flag
                    (event_id, market_id, bookmaker_name, active, flagged_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (event_id, market_id, bookmaker_name)
                DO UPDATE SET active = EXCLUDED.active, flagged_at = now()
                """,
                (event_id, market_id, book, active),
            )
        )
        self._flags = (0.0, set())

    # --- subscribers ----------------------------------------------------------------

    def subscribers(self) -> list[Subscriber]:
        rows = self._run(
            lambda c: c.execute(
                f"SELECT {_SUBSCRIBER_COLUMNS} FROM bot.subscriber WHERE active"
            ).fetchall()
        )
        return [Subscriber(*row) for row in rows]

    def subscriber(self, chat_id: int) -> Subscriber | None:
        row = self._run(
            lambda c: c.execute(
                f"SELECT {_SUBSCRIBER_COLUMNS} FROM bot.subscriber WHERE chat_id = %s", (chat_id,)
            ).fetchone()
        )
        return Subscriber(*row) if row else None

    def subscribe(self, chat_id: int, username: str | None) -> Subscriber:
        self._run(
            lambda c: c.execute(
                """
                INSERT INTO bot.subscriber (chat_id, username) VALUES (%s, %s)
                ON CONFLICT (chat_id) DO UPDATE
                  SET active = true, username = EXCLUDED.username, updated_at = now()
                """,
                (chat_id, username),
            )
        )
        found = self.subscriber(chat_id)
        assert found is not None
        return found

    _SETTABLE = {"active", "surebets", "ev", "ev_min", "stake", "muted_until"}

    def update(self, chat_id: int, **values: Any) -> Subscriber | None:
        unknown = set(values) - self._SETTABLE
        if unknown:
            raise ValueError(f"not settable: {unknown}")
        assignments = ", ".join(f"{k} = %s" for k in values)
        self._run(
            lambda c: c.execute(
                f"UPDATE bot.subscriber SET {assignments}, updated_at = now() WHERE chat_id = %s",
                (*values.values(), chat_id),
            )
        )
        return self.subscriber(chat_id)

    # --- the alert ledger -------------------------------------------------------------

    def ledger(self) -> Ledger:
        since = datetime.now(UTC) - ALERT_RETENTION
        rows = self._run(
            lambda c: c.execute(
                "SELECT chat_id, key, value FROM bot.alert WHERE alerted_at >= %s", (since,)
            ).fetchall()
        )
        return Ledger(sent={(int(chat), str(key)): float(value) for chat, key, value in rows})

    def record_alert(self, chat_id: int, key: str, value: float) -> None:
        self._run(
            lambda c: c.execute(
                """
                INSERT INTO bot.alert (chat_id, key, value, alerted_at) VALUES (%s, %s, %s, now())
                ON CONFLICT (chat_id, key) DO UPDATE
                  SET value = GREATEST(bot.alert.value, EXCLUDED.value), alerted_at = now()
                """,
                (chat_id, key, value),
            )
        )

    def prune_alerts(self) -> int:
        since = datetime.now(UTC) - ALERT_RETENTION
        return self._run(
            lambda c: c.execute("DELETE FROM bot.alert WHERE alerted_at < %s", (since,)).rowcount
        )
