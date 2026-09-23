"""Apply sql/supabase_bot_role.sql (part A) over the runner's own connection.

Run inside the runner container:
    docker exec arbibet-runner python scripts/apply_bot_role.py

It never touches a password: the role is created NOLOGIN, and part B (ALTER
ROLE ... LOGIN PASSWORD) is run by the owner in the Supabase SQL editor.
"""

from __future__ import annotations

import os
import pathlib

import psycopg

sql = (pathlib.Path(__file__).resolve().parents[1] / "sql" / "supabase_bot_role.sql").read_text(
    encoding="utf-8"
)
with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True) as conn:
    conn.execute(sql)
    cur = conn.execute(
        "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace "
        "WHERE nspname IN ('serving', 'ops', 'bot') ORDER BY 1"
    )
    print("schema owners:", cur.fetchall())
    cur = conn.execute(
        "SELECT schemaname, count(*) FROM pg_tables "
        "WHERE schemaname IN ('serving', 'ops', 'bot') "
        "AND tableowner = 'arbibet_runner' GROUP BY 1 ORDER BY 1"
    )
    print("tables owned by arbibet_runner:", cur.fetchall())
    cur = conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'arbibet_runner'")
    print("arbibet_runner can log in yet:", cur.fetchone()[0], "(False until part B)")
