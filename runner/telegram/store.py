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
    bot.balance      cash per (chat, mode, bookmaker); mode is real or paper
    bot.bet          every placed bet, real or paper, with its legs and, once
                     settled, each leg's verdict and payout
    bot.report       "odds changed" and "not on site" reports from alerts

Row-level security is enabled on both with NO policies: the dashboard's login
cannot read chat ids. The runner connects as the tables' owner.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import psycopg
from psycopg.types.json import Jsonb

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

CREATE TABLE IF NOT EXISTS bot.balance (
    chat_id     bigint NOT NULL,
    mode        text NOT NULL,
    book        text NOT NULL,
    amount      double precision NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, mode, book)
);

CREATE TABLE IF NOT EXISTS bot.bet (
    bet_id           bigserial PRIMARY KEY,
    chat_id          bigint NOT NULL,
    mode             text NOT NULL,
    kind             text NOT NULL,
    opportunity_key  text,
    event_id         text NOT NULL,
    market_id        text NOT NULL,
    fixture          text,
    market           text,
    kickoff_at       timestamptz,
    placed_at        timestamptz NOT NULL DEFAULT now(),
    source           text,
    status           text NOT NULL DEFAULT 'open',
    settled_at       timestamptz,
    profit           double precision,
    legs             jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS bet_chat ON bot.bet (chat_id, placed_at DESC);
ALTER TABLE bot.bet ADD COLUMN IF NOT EXISTS note text;
CREATE INDEX IF NOT EXISTS bet_open ON bot.bet (status, kickoff_at);

CREATE TABLE IF NOT EXISTS bot.report (
    report_id    bigserial PRIMARY KEY,
    chat_id      bigint NOT NULL,
    event_id     text NOT NULL,
    market_id    text NOT NULL,
    book         text NOT NULL,
    kind         text NOT NULL,
    odds         double precision,
    reported_at  timestamptz NOT NULL DEFAULT now()
);

DO $$
DECLARE t text; r text;
BEGIN
    FOREACH t IN ARRAY ARRAY['subscriber', 'alert', 'balance', 'bet', 'report'] LOOP
        EXECUTE format('ALTER TABLE bot.%I ENABLE ROW LEVEL SECURITY', t);
    END LOOP;
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
_BET_COLUMNS = (
    "bet_id, chat_id, mode, kind, opportunity_key, event_id, market_id, fixture, market, "
    "kickoff_at, placed_at, source, status, settled_at, profit, legs, note"
)


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

    # --- the wallet -------------------------------------------------------------------

    def balances(self, chat_id: int, mode: str) -> dict[str, float]:
        rows = self._run(
            lambda c: c.execute(
                "SELECT book, amount FROM bot.balance WHERE chat_id = %s AND mode = %s",
                (chat_id, mode),
            ).fetchall()
        )
        return {str(b): float(a) for b, a in rows}

    def set_balance(self, chat_id: int, mode: str, book: str, amount: float) -> None:
        self._run(
            lambda c: c.execute(
                """
                INSERT INTO bot.balance (chat_id, mode, book, amount) VALUES (%s, %s, %s, %s)
                ON CONFLICT (chat_id, mode, book)
                DO UPDATE SET amount = EXCLUDED.amount, updated_at = now()
                """,
                (chat_id, mode, book, amount),
            )
        )

    def move_balances(self, chat_id: int, mode: str, deltas: Mapping[str, float]) -> None:
        """Add each delta to its book's balance in one transaction (a missing row counts as 0)."""

        def go(c: psycopg.Connection) -> None:
            with c.transaction():
                for book, delta in deltas.items():
                    c.execute(
                        """
                        INSERT INTO bot.balance (chat_id, mode, book, amount)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (chat_id, mode, book)
                        DO UPDATE SET amount = bot.balance.amount + EXCLUDED.amount,
                                      updated_at = now()
                        """,
                        (chat_id, mode, book, delta),
                    )

        self._run(go)

    def _bet(self, row: tuple[Any, ...]) -> dict[str, Any]:
        keys = [k.strip() for k in _BET_COLUMNS.split(",")]
        return dict(zip(keys, row, strict=True))

    def add_bet(self, bet: Mapping[str, Any]) -> int:
        row = self._run(
            lambda c: c.execute(
                """
                INSERT INTO bot.bet (chat_id, mode, kind, opportunity_key, event_id, market_id,
                                     fixture, market, kickoff_at, placed_at, source, legs)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING bet_id
                """,
                (
                    bet["chat_id"],
                    bet["mode"],
                    bet["kind"],
                    bet.get("opportunity_key"),
                    bet["event_id"],
                    bet["market_id"],
                    bet.get("fixture"),
                    bet.get("market"),
                    bet.get("kickoff_at"),
                    bet["placed_at"],
                    bet.get("source"),
                    Jsonb(bet["legs"]),
                ),
            ).fetchone()
        )
        assert row is not None
        return int(row[0])

    def update_bet_legs(self, bet_id: int, legs: list[dict[str, Any]]) -> None:
        self._run(
            lambda c: c.execute(
                "UPDATE bot.bet SET legs = %s WHERE bet_id = %s", (Jsonb(legs), bet_id)
            )
        )

    def settle_bet(
        self, bet_id: int, legs: list[dict[str, Any]], profit: float, status: str = "settled"
    ) -> None:
        self._run(
            lambda c: c.execute(
                "UPDATE bot.bet SET legs = %s, profit = %s, status = %s, settled_at = now() "
                "WHERE bet_id = %s",
                (Jsonb(legs), profit, status, bet_id),
            )
        )

    def annotate_bet(self, bet_id: int, status: str, note: str) -> None:
        self._run(
            lambda c: c.execute(
                "UPDATE bot.bet SET status = %s, note = %s WHERE bet_id = %s",
                (status, note, bet_id),
            )
        )

    def bets(self, chat_id: int, mode: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._run(
            lambda c: c.execute(
                f"SELECT {_BET_COLUMNS} FROM bot.bet WHERE chat_id = %s "
                "AND (%s::text IS NULL OR mode = %s) ORDER BY placed_at DESC LIMIT %s",
                (chat_id, mode, mode, limit),
            ).fetchall()
        )
        return [self._bet(r) for r in rows]

    def bet(self, bet_id: int) -> dict[str, Any] | None:
        row = self._run(
            lambda c: c.execute(
                f"SELECT {_BET_COLUMNS} FROM bot.bet WHERE bet_id = %s", (bet_id,)
            ).fetchone()
        )
        return self._bet(row) if row else None

    def open_bets(self, kicked_off_before: datetime) -> list[dict[str, Any]]:
        rows = self._run(
            lambda c: c.execute(
                f"SELECT {_BET_COLUMNS} FROM bot.bet WHERE status = 'open' AND kickoff_at < %s "
                "ORDER BY kickoff_at",
                (kicked_off_before,),
            ).fetchall()
        )
        return [self._bet(r) for r in rows]

    def add_report(
        self,
        chat_id: int,
        event_id: str,
        market_id: str,
        book: str,
        kind: str,
        odds: float | None = None,
    ) -> None:
        self._run(
            lambda c: c.execute(
                "INSERT INTO bot.report (chat_id, event_id, market_id, book, kind, odds) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (chat_id, event_id, market_id, book, kind, odds),
            )
        )

    def counts(self) -> dict[str, int]:
        """For the owner: subscribers, signed users, bets and reports."""
        row = self._run(
            lambda c: c.execute(
                "SELECT (SELECT count(*) FROM bot.subscriber WHERE active), "
                "(SELECT count(DISTINCT chat_id) FROM bot.balance WHERE mode = 'real'), "
                "(SELECT count(*) FROM bot.bet WHERE mode = 'real'), "
                "(SELECT count(*) FROM bot.bet WHERE mode = 'paper'), "
                "(SELECT count(*) FROM bot.report)"
            ).fetchone()
        )
        assert row is not None
        keys = ("subscribers", "signed", "real_bets", "paper_bets", "reports")
        return dict(zip(keys, (int(v) for v in row), strict=True))
