"""Install the NOTIFY trigger on markets bronze (sql/bronze_notify.sql).

Idempotent: re-running replaces the function and trigger. Uses MARKETS_DB_URL,
in a read-write session, because the pipeline's own bronze connections are
read-only by design.

Run:
    python scripts/install_bronze_notify.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arbibet_capstone.env import load as load_env  # noqa: E402

load_env()

with psycopg.connect(os.environ["MARKETS_DB_URL"], autocommit=True) as conn:
    conn.execute("SET default_transaction_read_only = off")
    conn.execute((ROOT / "sql" / "bronze_notify.sql").read_text(encoding="utf-8"))
    found = conn.execute(
        "SELECT tgname FROM pg_trigger WHERE tgname = 'bronze_payload_notify'"
    ).fetchone()
    print("installed" if found else "NOT installed")
