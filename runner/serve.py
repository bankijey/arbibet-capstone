"""Publish what the dashboard shows to Supabase, the serving database.

The dashboard never reads the warehouse: DuckDB is a file on this machine, and
Streamlit runs in the cloud. So after the warehouse changes, the runner writes
the dashboard's data to Supabase's free Postgres, as JSON documents built by
the same functions the static web snapshot used (publish/snapshot.py):

    serving.document   one row per page section: 'signals' (headline numbers,
                       arbitrage, EV, backtest) and 'slips' (deep-dive list,
                       popular slips). Rewritten when its content changes.
    serving.dive       one row per fixture deep dive. Only new or changed
                       dives are written; older than 45 days are deleted.
    serving.leg_flag   viewer "not on the site" flags, written by the
                       dashboard, read back into the documents' flag list.
    ops.job_run        the runner's last 7 days of runs, and
    ops.heartbeat      its components' last beats -- for the Pipeline health
                       page, so the pipeline can be watched from anywhere.

SIZE. Supabase's free tier is 500 MB. The documents are a few MB, dives are
tens of KB each and Postgres compresses large jsonb values, and ops is pruned,
so this stays in the tens of MB. `publish` records the database size it sees
on every run, so growth is visible before it is a problem.

FRESHNESS. The warm loop publishes everything every 15 minutes. The hot loop
additionally asks for the 'signals' document after writing new signals; those
requests are coalesced to at most one publish every 30 seconds. Heartbeats and
changed job runs are mirrored every minute.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from arbibet_capstone.warehouse import Warehouse

log = logging.getLogger("runner.serve")

ROOT = Path(__file__).resolve().parents[1]
DIVE_RETENTION = timedelta(days=45)
OPS_RETENTION = timedelta(days=7)
SIGNALS_MIN_INTERVAL = 30.0

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS serving;
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS serving.document (
    name          text PRIMARY KEY,
    generated_at  timestamptz NOT NULL,
    digest        text NOT NULL,
    body          jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS serving.dive (
    event_id      text PRIMARY KEY,
    kickoff_at    timestamptz NOT NULL,
    digest        text NOT NULL,
    updated_at    timestamptz NOT NULL,
    body          jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS dive_kickoff ON serving.dive (kickoff_at);

CREATE TABLE IF NOT EXISTS serving.leg_flag (
    event_id        text NOT NULL,
    market_id       text NOT NULL,
    bookmaker_name  text NOT NULL,
    active          boolean NOT NULL,
    flagged_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, market_id, bookmaker_name)
);

CREATE TABLE IF NOT EXISTS ops.job_run (
    run_id        text PRIMARY KEY,
    job           text NOT NULL,
    loop          text NOT NULL,
    trigger       text,
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz,
    status        text NOT NULL,
    rows_written  bigint,
    detail        jsonb,
    error         text
);
CREATE INDEX IF NOT EXISTS job_run_started ON ops.job_run (started_at DESC);

CREATE TABLE IF NOT EXISTS ops.heartbeat (
    component     text PRIMARY KEY,
    beat_at       timestamptz NOT NULL,
    state         text,
    detail        jsonb
);

-- Row-level security on every serving table, with policies that give the
-- dashboard's login (sql/supabase_dashboard_role.sql) exactly what its grants
-- say: read everything, and insert or update flags. The runner connects as
-- the tables' owner, which RLS does not restrict unless FORCEd.
DO $$
DECLARE
    t record;
    has_reader boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_reader');
BEGIN
    FOR t IN
        SELECT n.nspname AS schema_name, c.relname AS table_name
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r' AND n.nspname IN ('serving', 'ops')
    LOOP
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', t.schema_name, t.table_name);
        IF has_reader AND NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = t.schema_name AND tablename = t.table_name
              AND policyname = 'dashboard_reader_select'
        ) THEN
            EXECUTE format(
                'CREATE POLICY dashboard_reader_select ON %I.%I '
                'FOR SELECT TO dashboard_reader USING (true)',
                t.schema_name, t.table_name
            );
        END IF;
    END LOOP;
    IF has_reader AND NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'serving' AND tablename = 'leg_flag'
          AND policyname = 'dashboard_reader_flag_insert'
    ) THEN
        CREATE POLICY dashboard_reader_flag_insert ON serving.leg_flag
            FOR INSERT TO dashboard_reader WITH CHECK (true);
        CREATE POLICY dashboard_reader_flag_update ON serving.leg_flag
            FOR UPDATE TO dashboard_reader USING (true) WITH CHECK (true);
    END IF;
END
$$;
"""


def configured() -> bool:
    return bool(os.environ.get("SUPABASE_DB_URL"))


def _connect() -> psycopg.Connection:
    # prepare_threshold=None: Supabase's transaction pooler cannot keep
    # server-side prepared statements across transactions.
    return psycopg.connect(
        os.environ["SUPABASE_DB_URL"], autocommit=True, prepare_threshold=None, connect_timeout=20
    )


def _snapshot() -> Any:
    from runner.jobs import _script

    return _script("publish/snapshot.py")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)


def _write_document(conn: psycopg.Connection, name: str, body: dict[str, Any]) -> bool:
    content = {k: v for k, v in body.items() if k != "generatedAt"}
    digest = _digest(content)
    current = conn.execute(
        "SELECT digest FROM serving.document WHERE name = %s", (name,)
    ).fetchone()
    if current and current[0] == digest:
        return False
    conn.execute(
        """
        INSERT INTO serving.document (name, generated_at, digest, body)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (name) DO UPDATE
          SET generated_at = EXCLUDED.generated_at, digest = EXCLUDED.digest, body = EXCLUDED.body
        """,
        (name, datetime.now(UTC), digest, Jsonb(body)),
    )
    return True


def _flags(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT event_id, market_id, bookmaker_name, flagged_at FROM serving.leg_flag WHERE active"
    ).fetchall()
    return [
        {"eventId": e, "marketId": m, "book": b, "flaggedAt": f.isoformat() if f else None}
        for e, m, b, f in rows
    ]


def publish_signals(warehouse: Warehouse, conn: psycopg.Connection | None = None) -> bool:
    """Only the 'signals' document: what the hot loop refreshes between warm cycles."""
    snap = _snapshot()
    wh = snap.Warehouse.__new__(snap.Warehouse)
    wh.conn = warehouse
    own = conn is None
    conn = conn or _connect()
    try:
        body: dict[str, Any] = {"generatedAt": snap._iso(pd.Timestamp.now(tz="UTC"))}
        body |= snap.headline(wh)
        body |= snap.signals(wh)
        body["arbitrage"]["flags"] = _flags(conn)
        return _write_document(conn, "signals", body)
    finally:
        if own:
            conn.close()


def publish(warehouse: Warehouse, run: Any, full: bool = False) -> int:
    """Both documents, changed dives, ops. Returns rows written.

    The warm loop rebuilds dives only for fixtures that can still change --
    upcoming, kicked off in the last three days, or not yet in Supabase.
    `full` (the cold loop) rebuilds every dive in the retention window.
    """
    snap = _snapshot()
    wh = snap.Warehouse.__new__(snap.Warehouse)
    wh.conn = warehouse
    written = 0
    with _connect() as conn:
        ensure_schema(conn)
        if publish_signals(warehouse, conn):
            written += 1

        slips = snap.slips(wh)
        now = pd.Timestamp.now(tz="UTC")
        if _write_document(conn, "slips", {"generatedAt": snap._iso(now), **slips}):
            written += 1

        # Dives: every slipped fixture inside the retention window.
        popular = pd.DataFrame(slips["popular"])
        dives_written = 0
        if not popular.empty:
            popular["kickoff"] = pd.to_datetime(popular.kickoffAt, utc=True)
            recent = popular[popular.kickoff > now - DIVE_RETENTION]
            digests = dict(conn.execute("SELECT event_id, digest FROM serving.dive").fetchall())
            if not full:
                live = recent.kickoff > now - timedelta(days=3)
                missing = ~recent.eventId.isin(list(digests))
                recent = recent[live | missing]
            ids = list(recent.eventId)
            for start in range(0, len(ids), 200):
                batch = snap.dives(wh, ids[start : start + 200])
                with conn.transaction():
                    for event_id, dive in batch.items():
                        digest = _digest(dive)
                        if digests.get(event_id) == digest:
                            continue
                        conn.execute(
                            """
                            INSERT INTO serving.dive
                                (event_id, kickoff_at, digest, updated_at, body)
                            VALUES (%s, %s, %s, now(), %s)
                            ON CONFLICT (event_id) DO UPDATE
                              SET kickoff_at = EXCLUDED.kickoff_at, digest = EXCLUDED.digest,
                                  updated_at = now(), body = EXCLUDED.body
                            """,
                            (event_id, dive["kickoffAt"], digest, Jsonb(dive)),
                        )
                        dives_written += 1
        conn.execute("DELETE FROM serving.dive WHERE kickoff_at < %s", (now - DIVE_RETENTION,))
        written += dives_written

        written += _publish_ops(warehouse, conn)
        size = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    run.detail |= {"dives_written": dives_written, "supabase_mb": round(size / 1e6, 1)}
    return written


_RUN_UPSERT = """
    INSERT INTO ops.job_run VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (run_id) DO UPDATE SET finished_at = EXCLUDED.finished_at,
      status = EXCLUDED.status, rows_written = EXCLUDED.rows_written,
      detail = EXCLUDED.detail, error = EXCLUDED.error
"""


def _mirror_runs(warehouse: Warehouse, conn: psycopg.Connection, changed_since: datetime) -> int:
    """Upsert the job runs that started or finished since `changed_since`, and any still running.

    Changed rows only: a minute's worth is a handful, where the whole 7-day
    window is ~15,000 rows.
    """
    runs = warehouse.query(
        "SELECT run_id, job, loop, trigger, started_at, finished_at, status, rows_written, "
        "detail::varchar AS detail, error FROM ops.job_run "
        "WHERE started_at >= %s "
        "AND (coalesce(finished_at, started_at) >= %s OR status = 'running')",
        (datetime.now(UTC) - OPS_RETENTION, changed_since),
    )
    if runs.empty:
        return 0
    rows = [
        (
            r.RUN_ID,
            r.JOB,
            r.LOOP,
            r.TRIGGER,
            _ts(r.STARTED_AT),
            _ts(r.FINISHED_AT),
            r.STATUS,
            None if pd.isna(r.ROWS_WRITTEN) else int(r.ROWS_WRITTEN),
            Jsonb(json.loads(r.DETAIL)) if isinstance(r.DETAIL, str) else None,
            r.ERROR if isinstance(r.ERROR, str) else None,
        )
        for r in runs.itertuples(index=False)
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(_RUN_UPSERT, rows)
    return len(rows)


def _mirror_beats(warehouse: Warehouse, conn: psycopg.Connection) -> None:
    beats = warehouse.query(
        "SELECT component, beat_at, state, detail::varchar AS detail FROM ops.heartbeat"
    )
    with conn.transaction():
        for b in beats.itertuples(index=False):
            _beat(conn, b.COMPONENT, _ts(b.BEAT_AT), b.STATE, b.DETAIL)


def _publish_ops(warehouse: Warehouse, conn: psycopg.Connection) -> int:
    """The warm publish's backstop for anything the minute mirror missed, and retention."""
    written = _mirror_runs(warehouse, conn, datetime.now(UTC) - timedelta(hours=2))
    _mirror_beats(warehouse, conn)
    conn.execute(
        "DELETE FROM ops.job_run WHERE started_at < %s", (datetime.now(UTC) - OPS_RETENTION,)
    )
    return written


def _ts(value: Any) -> datetime | None:
    return None if value is None or pd.isna(value) else pd.Timestamp(value).to_pydatetime()


def _beat(conn: psycopg.Connection, component: str, at: Any, state: Any, detail: Any) -> None:
    conn.execute(
        """
        INSERT INTO ops.heartbeat VALUES (%s, %s, %s, %s)
        ON CONFLICT (component) DO UPDATE
          SET beat_at = EXCLUDED.beat_at, state = EXCLUDED.state, detail = EXCLUDED.detail
        """,
        (component, at, state, Jsonb(json.loads(detail)) if isinstance(detail, str) else None),
    )


class SignalPublisher(threading.Thread):
    """Coalesces the hot loop's 'signals changed' requests into a publish at most every 30 s.

    Also mirrors the pipeline's own record every minute -- heartbeats, and the
    job runs that started or finished since the last mirror -- so the Pipeline
    health page is a minute behind the runner, not a warm cycle behind.
    """

    OPS_SECONDS = 60.0

    def __init__(self, warehouse: Warehouse, stop: threading.Event) -> None:
        super().__init__(name="serve", daemon=True)
        self.warehouse = warehouse
        self.stop = stop
        self.requested = threading.Event()
        # Where the next job-run mirror resumes; the first one covers the window.
        self.runs_since = datetime.now(UTC) - OPS_RETENTION

    def request(self, _rows: int = 0) -> None:
        self.requested.set()

    def mirror_ops(self, conn: psycopg.Connection) -> int:
        # A little overlap: a row finishing while the query runs must not be skipped.
        started = datetime.now(UTC) - timedelta(seconds=30)
        runs = _mirror_runs(self.warehouse, conn, self.runs_since)
        _mirror_beats(self.warehouse, conn)
        self.runs_since = started
        return runs

    def run(self) -> None:
        last_publish = 0.0
        last_ops = 0.0
        while not self.stop.is_set():
            self.requested.wait(timeout=self.OPS_SECONDS)
            if not configured():
                self.requested.clear()
                continue
            try:
                with _connect() as conn:
                    wait = SIGNALS_MIN_INTERVAL - (time.monotonic() - last_publish)
                    if self.requested.is_set() and wait <= 0:
                        self.requested.clear()
                        changed = publish_signals(self.warehouse, conn)
                        last_publish = time.monotonic()
                        log.info("signals document %s", "published" if changed else "unchanged")
                    elif self.requested.is_set():
                        self.stop.wait(wait)
                    if time.monotonic() - last_ops >= self.OPS_SECONDS:
                        self.mirror_ops(conn)
                        last_ops = time.monotonic()
            except Exception:
                log.warning("serving publish failed; will retry", exc_info=True)
                self.stop.wait(30)
