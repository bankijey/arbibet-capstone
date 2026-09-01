"""Read the latest payload per bookmaker for one fixture from markets bronze.

`bronze_event_payloads` is append-only and writes one row per *change* per
(event_id, bookmaker), so the newest row per book is that book's current price
picture for the fixture.

Access path: the query drives on `event_id`, the leading column of
`idx_bep_event_book_write (event_id, bookmaker, write_time DESC)`. Never add a
`bookmaker`-only filter — no index leads with that column, and the planner
falls back to a sequential scan over the BYTEA payload bodies. That is the
query shape that froze the production host mid-tournament.
"""

from __future__ import annotations

import os
from uuid import UUID

import psycopg

_LATEST_PER_BOOK = """
    SELECT DISTINCT ON (bookmaker) bookmaker, payload
    FROM bronze_event_payloads
    WHERE event_id = %s
    ORDER BY bookmaker, write_time DESC
"""


def latest_payloads(conn: psycopg.Connection, event_id: UUID) -> dict[str, bytes]:
    """The most recent payload each bookmaker published for `event_id`.

    Keys are bronze's own bookmaker names, which the parsers register under
    unchanged -- bronze sits downstream of arbibet-markets' alias mapping, so
    its spelling is the platform's canonical one. Values are the verbatim
    response bytes, undecoded: bronze's contract is byte fidelity, and a
    malformed body should fail in the parser, where that failure has a reason
    code, rather than here.

    Every book bronze holds is returned. Choosing which of them to parse is the
    caller's decision, not this function's.
    """
    with conn.cursor() as cur:
        cur.execute(_LATEST_PER_BOOK, (event_id,))
        return dict(cur)


def connect() -> psycopg.Connection:
    """Read-only session against the markets bronze database.

    Read-only is set on the session rather than left to convention: the
    capstone reads upstream and must never write to it, and a rail enforced by
    Postgres survives a careless edit in a way that a comment does not.
    """
    conn = psycopg.connect(os.environ["MARKETS_DB_URL"], autocommit=True)
    conn.execute("SET default_transaction_read_only = on")
    return conn
