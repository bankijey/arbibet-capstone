"""Connection to the Snowflake warehouse, and the idempotent write.

Every write in this pipeline lands here -- both consumers, both Spark jobs, the
summariser -- so the connection parameters are read in one place. The three
credentials are required and have no default; the four placement settings do,
because a wrong database is a mistake worth catching at connect rather than a
blank to fill in four .env files.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

import pandas as pd
import snowflake.connector
from snowflake.connector import SnowflakeConnection
from snowflake.connector.pandas_tools import write_pandas

from arbibet_capstone.env import require

# Every timestamp in this platform is Europe/Berlin, and this is where that is
# enforced for Snowflake.
#
# Snowflake's session TIMEZONE defaults to America/Los_Angeles, and TIMESTAMP_TZ
# and TIMESTAMP_LTZ are RENDERED in it. Leaving it at the default did not
# corrupt a single stored instant -- it made every one of them print nine hours
# early. Toulouse v Lille, a Ligue 1 evening fixture, displayed as an 11:45
# kick-off; it is 20:45 in Berlin, which is what markets bronze (a Postgres
# whose own timezone is Europe/Berlin) meant when it wrote the row.
#
# Berlin rather than UTC because bronze is the upstream source of truth for
# fire_time and it speaks Berlin. Two zones in one pipeline is how a chart's
# x-axis and its kick-off marker end up in different centuries of an argument.
TIMEZONE = "Europe/Berlin"


def connect() -> SnowflakeConnection:
    """A session against the capstone's warehouse, in platform time."""
    return snowflake.connector.connect(
        account=require("SNOWFLAKE_ACCOUNT"),
        user=require("SNOWFLAKE_USER"),
        password=require("SNOWFLAKE_PASSWORD"),
        role=os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "ARBIBET_CAPSTONE"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "CORE"),
        session_parameters={"TIMEZONE": TIMEZONE},
        # For the long-lived callers. A Snowflake session's token expires
        # after roughly four idle hours, and `watch/signals.py` holds one
        # connection for its entire run while deliberately going quiet
        # whenever there is no signal to write -- so it would wake up to
        # `390114: Authentication token has expired` on the first opportunity
        # it found all night, which is precisely the moment it must not fail.
        # The batch jobs finish long before this matters; the heartbeat costs
        # them nothing.
        client_session_keep_alive=True,
    )


# --- idempotent writes -------------------------------------------------------
#
# Snowflake enforces NOT NULL and nothing else: the PRIMARY KEY declarations in
# ddl.sql are documentation for readers and hints for the optimiser. So an
# INSERT would duplicate on every re-run, and the producer is deliberately
# re-runnable. Every write is a MERGE on a natural key -- the same idempotent
# convergence arbibet-silver's D4 states: re-running any event, after a crash
# or a full replay, converges to the same state.

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(name: str) -> str:
    """Validate a table or column name before it is interpolated into SQL.

    These names come from this codebase, never from data, so this is not an
    injection defence -- it is a typo trap. An identifier that reaches the
    warehouse malformed produces a Snowflake syntax error a hundred lines from
    the mistake that caused it.
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

    Key columns are matched on and never updated -- setting a row's identity to
    itself is noise at best. Columns absent from `columns` are left alone
    entirely, which is how `detected_at` keeps its original value across a
    re-run while still defaulting on first insert.
    """
    table = _identifier(table)
    cols = [_identifier(c) for c in columns]
    keys = [_identifier(k) for k in key]
    if not set(keys) <= set(cols):
        raise ValueError(f"key {keys} is not a subset of columns {cols}")

    # VARIANT columns need their JSON parsed on the way in; a bare bind would
    # store the text rather than a queryable object.
    source = ", ".join(
        f"PARSE_JSON(%s) AS {c}" if c in json_columns else f"%s AS {c}" for c in cols
    )
    return _merge_into(table, cols, keys, f"(SELECT {source})")


def _merge_into(
    table: str,
    columns: Sequence[str],
    keys: Sequence[str],
    source: str,
    keep: Sequence[str] = (),
) -> str:
    """The MERGE clauses, shared by the row-at-a-time and staged writers.

    They differ only in where the source rows come from -- an inline SELECT of
    bind parameters, or a staging table -- so the matching, updating and
    inserting is written once.

    Columns named in `keep` update to `COALESCE(s.c, t.c)`: a NULL arriving
    from the source means "not known on this read", never "known to be
    nothing", so it must not demote a value the warehouse already has.

    This is not hypothetical. `apifootball_events` is a ROLLING WINDOW -- it
    held fixture ids around 1,635,000 while the fixtures behind live booking
    slips needed 1,525,921 and 1,550,705, aged out weeks earlier. Those
    fixtures still carry an `apifootball` leg in the matcher, so the LEFT JOIN
    still runs and still produces a row; it simply produces NULL team ids. A
    plain `t.c = s.c` wrote those NULLs over ids `dim_fixture` had held since
    the fixture was fresh, and every slip leg pointing at them lost its
    settled history. The dashboard was the first thing to show it: six slip
    cards reading "0 with history" whose summaries, written a day earlier,
    cited form of 90% and 75%.

    Nothing errored, no test failed, and the row counts were unchanged. The
    only visible symptom was a number quietly becoming NULL.
    """
    on = " AND ".join(f"t.{k} = s.{k}" for k in keys)
    keepers = set(keep)
    updates = ", ".join(
        f"t.{c} = COALESCE(s.{c}, t.{c})" if c in keepers else f"t.{c} = s.{c}"
        for c in columns
        if c not in keys
    )
    inserts = ", ".join(columns)
    values = ", ".join(f"s.{c}" for c in columns)
    return (
        f"MERGE INTO {table} t USING {source} s ON {on} "
        f"WHEN MATCHED THEN UPDATE SET {updates} "
        f"WHEN NOT MATCHED THEN INSERT ({inserts}) VALUES ({values})"
    )


def merge(
    conn: SnowflakeConnection,
    *,
    table: str,
    rows: Sequence[Mapping[str, Any]],
    key: Sequence[str],
    json_columns: Collection[str] = (),
) -> int:
    """Upsert `rows` into `table`, matching on `key`. Returns rows written.

    Every row must carry the same columns in the same order: one statement is
    built for the whole batch, so a row of a different shape would bind its
    values into the wrong places rather than fail.
    """
    if not rows:
        return 0

    columns = list(rows[0])
    for i, row in enumerate(rows[1:], start=1):
        if list(row) != columns:
            raise ValueError(f"row {i} has columns {list(row)}, expected {columns}")

    sql = _merge_sql(table, columns, key, json_columns)
    params = [
        tuple(json.dumps(row[c]) if c in json_columns else row[c] for c in columns)
        for row in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, params)
    return len(rows)


def bookmaker_ids(conn: SnowflakeConnection) -> dict[str, int]:
    """`{bookmaker_name: bookmaker_id}` from `dim_bookmaker`.

    Read rather than hardcoded. The seed lives in ddl.sql; a second copy here
    would be the same knowledge in two places, and the two would disagree the
    first time a book is added. Five rows, once per run.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT bookmaker_name, bookmaker_id FROM dim_bookmaker")
        ids = {str(name): int(bid) for name, bid in cur.fetchall()}
    if not ids:
        raise RuntimeError("dim_bookmaker is empty -- run snowflake/apply_ddl.py")
    return ids


def merge_bulk(
    conn: SnowflakeConnection,
    *,
    table: str,
    rows: Sequence[Mapping[str, Any]],
    key: Sequence[str],
    keep: Sequence[str] = (),
) -> int:
    """Upsert many rows in one round trip, via a temporary staging table.

    `merge()` sends one statement per row, which is right for a consumer
    writing a handful of signals per message and hopeless for a load: 4,527
    dimension rows took long enough to be killed mid-flight, leaving the table
    silently partial. This stages the whole batch with `write_pandas` -- a PUT
    and a COPY, not 4,527 round trips -- then MERGEs once.

    The staging table is TEMPORARY, so it is scoped to this session and
    disappears with it; there is no cleanup to forget and no name to collide.
    Columns the caller does not supply are simply not referenced, so the
    target's defaults still apply on insert and its existing values survive an
    update.

    No VARIANT support, deliberately: nothing loaded in bulk here has one, and
    a JSON column would need PARSE_JSON on the way out of staging. Use
    `merge()` for those.
    """
    if not rows:
        return 0

    columns = [_identifier(c) for c in rows[0]]
    keys = [_identifier(k) for k in key]
    if not set(keys) <= set(columns):
        raise ValueError(f"key {keys} is not a subset of columns {columns}")

    table = _identifier(table)
    stage = f"{table}_stage"
    frame = pd.DataFrame(list(rows), columns=columns)

    with conn.cursor() as cur:
        cur.execute(f"CREATE OR REPLACE TEMPORARY TABLE {stage} LIKE {table}")
    # use_logical_type keeps timezone-aware timestamps correct. Without it the
    # connector warns that they "can result in datetimes being incorrectly
    # written" -- a silent shift on kickoff_at would move every fixture into
    # the wrong hour and every downstream window with it.
    write_pandas(
        conn, frame, stage.upper(), quote_identifiers=False, use_logical_type=True
    )

    with conn.cursor() as cur:
        cur.execute(_merge_into(table, columns, keys, stage, keep))
    return len(rows)
