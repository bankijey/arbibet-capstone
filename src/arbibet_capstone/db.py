"""Connections to the upstream Arbibet databases.

This module exists because a second database arrived. `bronze.py` held its own
`connect()` while markets bronze was the only upstream; the read-only rail is
now needed twice, and a rule enforced in one place is a rule, while the same
rule written twice is a coincidence waiting to diverge.

Each caller names its own DSN variable, so the environment-variable name still
lives beside the module that owns that database.
"""

from __future__ import annotations

import psycopg

from arbibet_capstone.env import require


def connect(dsn_env_var: str) -> psycopg.Connection:
    """A read-only session against the database named by `dsn_env_var`.

    Read-only is set on the session rather than left to convention: the
    capstone reads upstream and must never write to it, and a rail enforced by
    Postgres survives a careless edit in a way that a comment does not.
    """
    conn = psycopg.connect(require(dsn_env_var), autocommit=True)
    conn.execute("SET default_transaction_read_only = on")
    return conn
