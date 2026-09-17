"""One-off: copy every CORE table from Snowflake into the local DuckDB warehouse.

ANALYTICS is not copied: dbt rebuilds it from CORE. Snowflake itself is left
untouched -- it is kept as the capstone's record of the cloud deployment.

Each table is copied in Arrow batches and replaced wholesale, so the script can
be re-run until the switch-over, and verified row count by row count.

Run:
    python scripts/copy_snowflake_to_duckdb.py [table ...]
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
from arbibet_capstone.warehouse import connect  # noqa: E402

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("copy")
logging.getLogger("snowflake").setLevel(logging.WARNING)


def main() -> int:
    wanted = {t.lower() for t in sys.argv[1:]}
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
    targets = [
        name
        for (name,) in wh.raw.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'core' ORDER BY 1"
        ).fetchall()
        if not wanted or name.lower() in wanted
    ]
    total = 0
    for table in targets:
        columns = [
            (name, dtype)
            for name, dtype in wh.raw.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'core' AND table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()
        ]
        cur = source.cursor()
        try:
            cur.execute(f"SELECT * FROM CORE.{table}")
        except snowflake.connector.errors.ProgrammingError as err:
            log.warning("%s: not in Snowflake (%s), skipped", table, err.msg)
            continue
        present = {d[0].lower() for d in cur.description}
        picked = [(n, t) for n, t in columns if n.lower() in present]
        wh.raw.execute(f"DELETE FROM core.{table}")
        copied = 0
        for i, batch in enumerate(cur.fetch_pandas_batches()):
            batch.columns = [c.lower() for c in batch.columns]
            view = f"_copy_{table}_{i}"
            wh.raw.register(view, batch)
            select = ", ".join(
                f"CAST({n} AS JSON)" if t == "JSON" else f"CAST({n} AS {t})" for n, t in picked
            )
            wh.raw.execute(
                f"INSERT INTO core.{table} ({', '.join(n for n, _ in picked)}) "
                f"SELECT {select} FROM {view}"
            )
            wh.raw.unregister(view)
            copied += len(batch)
        cur.execute(f"SELECT count(*) FROM CORE.{table}")
        expected = cur.fetchone()[0]
        got = wh.raw.execute(f"SELECT count(*) FROM core.{table}").fetchone()[0]
        status = "OK" if got == expected else "MISMATCH"
        log.info("%-28s snowflake=%-9d duckdb=%-9d %s", table, expected, got, status)
        total += got
        if got != expected:
            return 1
    wh.raw.execute("CHECKPOINT")
    log.info("copied %d rows into %s", total, os.environ.get("DUCKDB_PATH", "data/arbibet.duckdb"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
