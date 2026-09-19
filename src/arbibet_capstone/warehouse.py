"""The local DuckDB warehouse, and the idempotent write.

Every write in this pipeline lands here, so the connection is made in one
place. It replaced Snowflake in September 2026: 221 MB of data did not need a
cloud warehouse, and the metered compute had cost $212 of trial credit in 16
days (snowflake/ddl.sql stays as the record of that deployment).

ONE PROCESS OWNS THE FILE. DuckDB lets a single process open a database for
writing. The runner (runner/main.py) is that process, and every job it runs --
signals, dbt, the Spark jobs, the summaries -- runs inside it and shares the
connection below. Two scripts opening the file at once fail with a lock error
rather than corrupting anything, which is the right failure.

The wrapper keeps the calling convention the Snowflake connector had --
`with connect() as wh`, `wh.cursor()`, `%s` placeholders -- so the jobs did not
need rewriting to move.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

# Every timestamp in this platform is Europe/Berlin. Bronze is the upstream
# source of truth for fire_time and it speaks Berlin; a TIMESTAMPTZ renders in
# the session zone, so the session is set to it on every connection.
TIMEZONE = "Europe/Berlin"

ROOT = Path(__file__).resolve().parents[2]
DDL = ROOT / "sql" / "duckdb_ddl.sql"
DEFAULT_PATH = ROOT / "data" / "arbibet.duckdb"
DEFAULT_LAKE = ROOT / "data" / "lake"

_lock = threading.Lock()
_database: duckdb.DuckDBPyConnection | None = None


def database_path() -> Path:
    return Path(os.environ.get("DUCKDB_PATH", str(DEFAULT_PATH)))


def lake_path() -> Path:
    return Path(os.environ.get("LAKE_PATH", str(DEFAULT_LAKE)))


def _open() -> duckdb.DuckDBPyConnection:
    global _database
    with _lock:
        if _database is None:
            path = database_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = duckdb.connect(str(path))
            conn.execute(f"SET TimeZone = '{TIMEZONE}'")
            # Bounded, and allowed to spill to disk. DuckDB's default is 80% of
            # the memory it can see; inside Docker that is the whole VM shared
            # with bronze's Postgres and the collectors, and parsing every slip
            # payload's JSON (stg_slip_leg) ran it out at 6.1 GB. dbt runs in
            # this process and shares these settings.
            conn.execute(f"SET memory_limit = '{os.environ.get('DUCKDB_MEMORY_LIMIT', '3GB')}'")
            conn.execute(f"SET threads = {int(os.environ.get('DUCKDB_THREADS', '2'))}")
            conn.execute("SET preserve_insertion_order = false")
            spill = path.parent / "tmp"
            spill.mkdir(parents=True, exist_ok=True)
            conn.execute(f"SET temp_directory = '{spill.as_posix()}'")
            conn.execute(DDL.read_text(encoding="utf-8"))
            _create_lake_views(conn)
            # Unqualified names resolve the way they did in Snowflake, where
            # the session schema was CORE.
            conn.execute("SET search_path = 'core,analytics,ops,main'")
            _database = conn
        return _database


class Cursor:
    """A DuckDB cursor that accepts the Snowflake connector's `%s` style."""

    def __init__(self, raw: duckdb.DuckDBPyConnection) -> None:
        self._raw = raw
        self._raw.execute(f"SET TimeZone = '{TIMEZONE}'")
        self._raw.execute("SET search_path = 'core,analytics,ops,main'")

    @staticmethod
    def _sql(sql: str, params: Any) -> str:
        return sql.replace("%s", "?") if params is not None else sql

    _DML = re.compile(r"^\s*(UPDATE|DELETE|INSERT|MERGE)\b", re.IGNORECASE)

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Cursor:
        if params is None:
            self._raw.execute(sql)
        else:
            self._raw.execute(self._sql(sql, params), list(params))
        # DuckDB returns a DML statement's affected-row count as its one result
        # row; the Snowflake connector exposed it as `rowcount`.
        self._rowcount = -1
        if self._DML.match(sql):
            counted = self._raw.fetchone()
            self._rowcount = int(counted[0]) if counted else 0
        return self

    def executemany(self, sql: str, params: Sequence[Sequence[Any]]) -> Cursor:
        self._raw.executemany(self._sql(sql, params), [list(p) for p in params])
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._raw.fetchall()

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._raw.fetchone()

    def df(self) -> pd.DataFrame:
        return self._raw.df()

    @property
    def description(self) -> Any:
        # Upper-cased, as Snowflake returned unquoted identifiers: callers
        # build dicts from these names and read `row["KICKOFF_AT"]`.
        described = self._raw.description
        if described is None:
            return None
        return [(str(d[0]).upper(), *d[1:]) for d in described]

    @property
    def rowcount(self) -> int:
        return getattr(self, "_rowcount", -1)

    def close(self) -> None:
        self._raw.close()

    def __iter__(self):
        return iter(self._raw.fetchall())

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Warehouse:
    """The shared connection, handed out by `connect()`.

    Leaving a `with connect()` block does NOT close it: the process keeps one
    connection for its life, because reopening a DuckDB file per job would
    re-run the DDL and, worse, fight any other job still holding it.
    """

    def __init__(self, raw: duckdb.DuckDBPyConnection) -> None:
        self.raw = raw

    def cursor(self) -> Cursor:
        return Cursor(self.raw.cursor())

    def query(self, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
        """A SELECT as a DataFrame, with UPPERCASE column names.

        Snowflake returned unquoted identifiers in upper case and every caller
        reads them that way (`row["KICKOFF_AT"]`, `frame.EVENT_ID`). DuckDB
        keeps the case they were written in, so it is normalised here once.
        """
        with self.cursor() as cur:
            cur.execute(sql, params)
            frame = cur.df()
        frame.columns = [str(c).upper() for c in frame.columns]
        return frame

    def close(self) -> None:
        """No-op: see the class docstring."""

    def __enter__(self) -> Warehouse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def connect() -> Warehouse:
    """The process's warehouse connection, opened (and schema applied) on first use."""
    return Warehouse(_open())


# --- idempotent writes -------------------------------------------------------
#
# No key constraints exist (see sql/duckdb_ddl.sql), so an INSERT would
# duplicate on every re-run. Every write is a MERGE on a natural key, so
# re-running any event, after a crash or a full replay, converges to the same
# state.

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(name: str) -> str:
    """Validate a table or column name before it is interpolated into SQL.

    These names come from this codebase, never from data, so this is not an
    injection defence -- it is a typo trap.
    """
    if not _IDENTIFIER.match(name):
        raise ValueError(f"not a valid SQL identifier: {name!r}")
    return name


def _merge_sql(
    table: str,
    columns: Sequence[str],
    key: Sequence[str],
    json_columns: Collection[str],
) -> str:
    """The MERGE statement for one row of `columns` into `table`.

    Key columns are matched on and never updated. Columns absent from
    `columns` are left alone entirely, which is how `detected_at` keeps its
    original value across a re-run while still defaulting on first insert.
    """
    table = _identifier(table)
    cols = [_identifier(c) for c in columns]
    keys = [_identifier(k) for k in key]
    if not set(keys) <= set(cols):
        raise ValueError(f"key {keys} is not a subset of columns {cols}")
    source = ", ".join(
        f"CAST(%s AS JSON) AS {c}" if c in json_columns else f"%s AS {c}" for c in cols
    )
    return _merge_into(table, cols, keys, f"(SELECT {source})")


def _merge_into(
    table: str,
    columns: Sequence[str],
    keys: Sequence[str],
    source: str,
    keep: Sequence[str] = (),
) -> str:
    """The MERGE clauses, shared by the row-at-a-time and bulk writers.

    Columns named in `keep` update to `COALESCE(s.c, t.c)`: a NULL arriving
    from the source means "not known on this read", never "known to be
    nothing", so it must not demote a value the warehouse already has.

    This is not hypothetical. `apifootball_events` is a ROLLING WINDOW, and a
    fixture that aged out of it still produced a row -- with NULL team ids. A
    plain `t.c = s.c` wrote those NULLs over ids `dim_fixture` had held since
    the fixture was fresh, and every slip leg pointing at them lost its settled
    history. Nothing errored and the row counts were unchanged.
    """
    on = " AND ".join(f"t.{k} = s.{k}" for k in keys)
    keepers = set(keep)
    updates = ", ".join(
        f"{c} = COALESCE(s.{c}, t.{c})" if c in keepers else f"{c} = s.{c}"
        for c in columns
        if c not in keys
    )
    inserts = ", ".join(columns)
    values = ", ".join(f"s.{c}" for c in columns)
    matched = f"WHEN MATCHED THEN UPDATE SET {updates} " if updates else ""
    return (
        f"MERGE INTO {table} t USING {source} s ON {on} "
        f"{matched}"
        f"WHEN NOT MATCHED THEN INSERT ({inserts}) VALUES ({values})"
    )


def merge(
    conn: Warehouse,
    *,
    table: str,
    rows: Sequence[Mapping[str, Any]],
    key: Sequence[str],
    json_columns: Collection[str] = (),
) -> int:
    """Upsert `rows` into `table`, matching on `key`. Returns rows written.

    Kept for callers that write a handful of rows. Locally a MERGE per row is
    cheap, but `merge_bulk` is one statement and is what anything larger uses.
    """
    return merge_bulk(conn, table=table, rows=rows, key=key, json_columns=json_columns)


def bookmaker_ids(conn: Warehouse) -> dict[str, int]:
    """`{bookmaker_name: bookmaker_id}` from `dim_bookmaker`, seeded by the DDL."""
    with conn.cursor() as cur:
        cur.execute("SELECT bookmaker_name, bookmaker_id FROM core.dim_bookmaker")
        ids = {str(name): int(bid) for name, bid in cur.fetchall()}
    if not ids:
        raise RuntimeError("dim_bookmaker is empty -- the DDL seed did not run")
    return ids


def merge_bulk(
    conn: Warehouse,
    *,
    table: str,
    rows: Sequence[Mapping[str, Any]],
    key: Sequence[str],
    keep: Sequence[str] = (),
    json_columns: Collection[str] = (),
) -> int:
    """Upsert many rows in one statement, from a DataFrame registered in-process.

    Every row must carry the same columns. Rows are de-duplicated on `key`
    (last one wins) first: a MERGE whose source holds a key twice would update
    the same target row twice, which DuckDB refuses.

    JSON columns are serialised to text and cast on the way in, because a dict
    in a DataFrame is not a JSON value to DuckDB.
    """
    if not rows:
        return 0

    columns = [_identifier(c) for c in rows[0]]
    for i, row in enumerate(rows[1:], start=1):
        if list(row) != columns:
            raise ValueError(f"row {i} has columns {list(row)}, expected {columns}")
    keys = [_identifier(k) for k in key]
    if not set(keys) <= set(columns):
        raise ValueError(f"key {keys} is not a subset of columns {columns}")

    table = _identifier(table)
    as_json = {_identifier(c) for c in json_columns}
    frame = pd.DataFrame(list(rows), columns=columns)
    for c in as_json:
        frame[c] = frame[c].map(lambda v: None if v is None else json.dumps(v, default=str))
    frame = frame.drop_duplicates(subset=keys, keep="last")

    view = f"_stage_{table}_{threading.get_ident()}"
    picked = ", ".join(f"CAST({c} AS JSON) AS {c}" if c in as_json else c for c in columns)
    raw = conn.raw.cursor()
    try:
        raw.execute(f"SET TimeZone = '{TIMEZONE}'")
        raw.execute("SET search_path = 'core,analytics,ops,main'")
        raw.register(view, frame)
        target = table if "." in table else _qualified(raw, table)
        raw.execute(_merge_into(target, columns, keys, f"(SELECT {picked} FROM {view})", keep))
    finally:
        try:
            raw.unregister(view)
        finally:
            raw.close()
    return len(frame)


def _qualified(raw: duckdb.DuckDBPyConnection, table: str) -> str:
    """`schema.table` for an unqualified pipeline table (core, else ops)."""
    found = raw.execute(
        "SELECT table_schema FROM information_schema.tables "
        "WHERE lower(table_name) = lower(?) AND table_schema IN ('core', 'ops', 'analytics') "
        "ORDER BY CASE table_schema WHEN 'core' THEN 0 WHEN 'ops' THEN 1 ELSE 2 END LIMIT 1",
        [table],
    ).fetchone()
    return f"{found[0]}.{table}" if found else table


# --- the lake: large raw payloads, as Parquet outside the database -----------
#
# Booking-slip payloads are ~110 KB of JSON each. DuckDB stores strings that
# long uncompressed, and 18k versions made a 3.9 GB database; the same rows as
# zstd Parquet are 4 MB. So they are written as Parquet, partitioned by month,
# and read through a view that looks exactly like the old table.

SLIP_TABLE = "bronze_slip_payload"
_SLIP_COLUMNS = {
    "source": "VARCHAR",
    "share_code": "VARCHAR",
    "payload_hash": "VARCHAR",
    "payload": "JSON",
    "list_id": "VARCHAR",
    "followed_times": "BIGINT",
    "folds": "BIGINT",
    "first_fetched_at": "TIMESTAMPTZ",
    "last_fetched_at": "TIMESTAMPTZ",
}


def _slip_dir() -> Path:
    return lake_path() / SLIP_TABLE


def _parquet(path: Path) -> str:
    return str(path).replace("\\", "/").replace("'", "''")


def _create_lake_views(conn: duckdb.DuckDBPyConnection) -> None:
    folder = _slip_dir()
    if not any(folder.glob("month=*/*.parquet")):
        # read_parquet refuses an empty glob, so an empty but correctly typed
        # file stands in until the first fetch.
        empty = folder / "month=0000-00"
        empty.mkdir(parents=True, exist_ok=True)
        columns = ", ".join(f"CAST(NULL AS {t}) AS {c}" for c, t in _SLIP_COLUMNS.items())
        conn.execute(
            f"COPY (SELECT {columns} LIMIT 0) TO '{_parquet(empty / 'empty.parquet')}' "
            "(FORMAT parquet)"
        )
    typed = ", ".join(f"CAST({c} AS {t}) AS {c}" for c, t in _SLIP_COLUMNS.items())
    files = f"{_parquet(folder)}/month=*/*.parquet"
    conn.execute(
        f"""
        CREATE OR REPLACE VIEW core.{SLIP_TABLE} AS
        WITH versions AS (
            SELECT {typed}
            FROM read_parquet('{files}', union_by_name = true)
        )
        SELECT * REPLACE (
            min(first_fetched_at) OVER (
                PARTITION BY source, share_code, payload_hash) AS first_fetched_at
        )
        FROM versions
        QUALIFY row_number() OVER (
            PARTITION BY source, share_code, payload_hash ORDER BY last_fetched_at DESC
        ) = 1
        """
    )
    # Each slip as it now stands. Finds the newest version by its small columns
    # first and only then reads that row's payload: selecting whole rows made
    # the engine decompress and validate all ~18k payloads (38 s) to keep 3.4k
    # of them; this is ~6 s.
    conn.execute(
        f"""
        CREATE OR REPLACE VIEW core.{SLIP_TABLE}_latest AS
        WITH newest AS (
            SELECT filename, file_row_number
            FROM read_parquet('{files}', filename = true, file_row_number = true)
            QUALIFY row_number() OVER (
                PARTITION BY source, share_code ORDER BY last_fetched_at DESC
            ) = 1
        )
        SELECT p.* EXCLUDE (filename, file_row_number, month)
        FROM read_parquet('{files}', filename = true, file_row_number = true,
                          hive_partitioning = true, union_by_name = true) p
        SEMI JOIN newest n USING (filename, file_row_number)
        """
    )


def append_slips(conn: Warehouse, rows: Sequence[Mapping[str, Any]]) -> int:
    """Append slip payload versions to the lake. Returns rows written.

    `first_fetched_at` defaults to `last_fetched_at` when the caller leaves it
    out; the view takes the earliest across every copy of a version.
    """
    if not rows:
        return 0
    frame = pd.DataFrame(list(rows))
    if "first_fetched_at" not in frame:
        frame["first_fetched_at"] = frame["last_fetched_at"]
    frame["payload"] = frame["payload"].map(lambda v: json.dumps(v, default=str))
    for column in _SLIP_COLUMNS:
        if column not in frame:
            frame[column] = None
    frame = frame[list(_SLIP_COLUMNS)]
    typed = ", ".join(f"CAST({c} AS {t}) AS {c}" for c, t in _SLIP_COLUMNS.items())
    raw = conn.raw.cursor()
    view = f"_slips_{threading.get_ident()}"
    try:
        raw.execute(f"SET TimeZone = '{TIMEZONE}'")
        raw.register(view, frame)
        raw.execute(
            f"COPY (SELECT {typed}, strftime(last_fetched_at, '%Y-%m') AS month FROM {view}) "
            f"TO '{_parquet(_slip_dir())}' (FORMAT parquet, COMPRESSION zstd, "
            "PARTITION_BY (month), APPEND, FILENAME_PATTERN 'part_{uuid}')"
        )
        raw.unregister(view)
    finally:
        raw.close()
    return len(frame)


def compact_slips(conn: Warehouse) -> int:
    """Rewrite each month's small files as one. Returns files removed.

    Every fetch appends a file; left alone, a year of hourly fetches is 8,760
    of them and every query opens them all. Written to a temporary name first
    and swapped in, so a crash mid-compaction leaves duplicates (which the view
    already collapses) rather than a gap.
    """
    removed = 0
    raw = conn.raw.cursor()
    try:
        for month in sorted(_slip_dir().glob("month=*")):
            files = sorted(month.glob("*.parquet"))
            if len(files) <= 1:
                continue
            target = month / "compacted.parquet.tmp"
            # No ORDER BY: sorting rows that each carry a slip's whole JSON
            # payload ran DuckDB out of its 2.7 GiB cap on 66 files (11 MB on
            # disk). Nothing depends on the order within a file -- the views
            # pick rows by last_fetched_at -- and unsorted, the copy streams.
            # Small row groups keep the writer's buffer small for the same reason.
            raw.execute(
                f"COPY (SELECT * FROM read_parquet('{_parquet(month)}/*.parquet', "
                f"union_by_name = true)) TO '{_parquet(target)}' "
                "(FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 9, ROW_GROUP_SIZE 2048)"
            )
            final = month / f"compacted_{len(files)}_{os.getpid()}.parquet"
            target.rename(final)
            for f in files:
                f.unlink()
                removed += 1
    finally:
        raw.close()
    return removed
