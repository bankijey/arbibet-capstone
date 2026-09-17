"""One-off, at switch-over: bring DuckDB level with Snowflake's last writes.

`copy_snowflake_to_duckdb.py` made the first copy. Airflow kept writing to
Snowflake until the switch, and the runner's test runs wrote to DuckDB, so a
blind replace would lose the second. Per table:

    merge    signals, ticks, arbitrage track, AI summaries, cursors -- keyed
             rows either side may have written; cursors keep the later position
    append   booking-slip payloads fetched after the first copy (the lake view
             collapses duplicates)
    replace  everything else: dimensions, current state, history

Run once, with Airflow stopped and the runner not running.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import snowflake.connector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arbibet_capstone.env import load as load_env  # noqa: E402
from arbibet_capstone.warehouse import (  # noqa: E402
    _merge_into,
    append_slips,
    connect,
)

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync")
logging.getLogger("snowflake").setLevel(logging.WARNING)

MERGE = {
    "fact_arbitrage_signal": ["signal_key"],
    "fact_ev_signal": ["signal_key"],
    "fact_odds_tick": ["event_id", "market_id", "outcome_id", "bookmaker_id", "fire_time"],
    "fact_arbitrage_track": ["event_id", "market_id", "observed_at"],
    "gold_slip_summary_ai": ["share_code", "leg_signature"],
    "gold_fixture_summary_ai": ["event_id", "evidence_signature"],
    "gold_fixture_result_ai": ["event_id"],
}
SLIPS_AFTER = "2026-09-17 08:40:00 +00:00"  # the first copy started 08:48 UTC


def main() -> int:
    source = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role=os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        database="ARBIBET_CAPSTONE",
        schema="CORE",
        session_parameters={"TIMEZONE": "Europe/Berlin"},
    )
    wh = connect()
    raw = wh.raw
    tables = [
        t
        for (t,) in raw.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'core' AND table_type = 'BASE TABLE' ORDER BY 1"
        ).fetchall()
    ]
    for table in tables:
        columns = raw.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'core' AND table_name = ? ORDER BY ordinal_position",
            [table],
        ).fetchall()
        cur = source.cursor()
        cur.execute(f"SELECT * FROM CORE.{table}")
        present = {d[0].lower() for d in cur.description}
        picked = [(n, t) for n, t in columns if n.lower() in present]
        names = [n for n, _ in picked]
        select = ", ".join(
            f"CAST({n} AS JSON) AS {n}" if t == "JSON" else f"CAST({n} AS {t}) AS {n}"
            for n, t in picked
        )
        staged = f"_sync_{table}"
        raw.execute(
            f"CREATE OR REPLACE TEMP TABLE {staged} AS SELECT {select} "
            f"FROM core.{table} LIMIT 0"
        )
        for i, batch in enumerate(cur.fetch_pandas_batches()):
            batch.columns = [c.lower() for c in batch.columns]
            raw.register(f"_b{i}", batch)
            raw.execute(f"INSERT INTO {staged} SELECT {select} FROM _b{i}")
            raw.unregister(f"_b{i}")
        before = raw.execute(f"SELECT count(*) FROM core.{table}").fetchone()[0]
        incoming = raw.execute(f"SELECT count(*) FROM {staged}").fetchone()[0]
        if table in MERGE:
            key = MERGE[table]
            raw.execute(
                _merge_into(
                    f"core.{table}",
                    names,
                    key,
                    f"(SELECT * FROM {staged} QUALIFY row_number() OVER "
                    f"(PARTITION BY {', '.join(key)}) = 1)",
                )
            )
            mode = "merge"
        elif table == "pipeline_cursor":
            raw.execute(
                f"""
                MERGE INTO core.pipeline_cursor t USING {staged} s
                  ON t.scope = s.scope AND t.key = s.key
                WHEN MATCHED AND s.position > t.position
                  THEN UPDATE SET position = s.position, updated_at = s.updated_at
                WHEN NOT MATCHED THEN INSERT BY NAME
                """
            )
            mode = "merge (later position wins)"
        else:
            raw.execute(f"DELETE FROM core.{table}")
            raw.execute(f"INSERT INTO core.{table} ({', '.join(names)}) SELECT * FROM {staged}")
            mode = "replace"
        after = raw.execute(f"SELECT count(*) FROM core.{table}").fetchone()[0]
        log.info("%-26s %-28s snowflake=%-8d duckdb %d -> %d", table, mode, incoming, before, after)
        raw.execute(f"DROP TABLE {staged}")

    cur = source.cursor()
    cur.execute(
        "SELECT source, share_code, payload_hash, payload::string AS payload, list_id, "
        "followed_times, folds, first_fetched_at, last_fetched_at "
        f"FROM CORE.bronze_slip_payload WHERE last_fetched_at > '{SLIPS_AFTER}'::timestamp_tz"
    )
    import json

    rows = [
        {**dict(zip([d[0].lower() for d in cur.description], r, strict=True))}
        for r in cur.fetchall()
    ]
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    log.info("bronze_slip_payload appended %d versions", append_slips(wh, rows))
    raw.execute("CHECKPOINT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
