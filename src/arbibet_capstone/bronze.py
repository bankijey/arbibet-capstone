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

from datetime import datetime
from typing import NamedTuple
from uuid import UUID

import psycopg

from arbibet_capstone.db import connect as _connect


class BookPayload(NamedTuple):
    """One bookmaker's most recent payload for a fixture, and when it was fired.

    `fire_time` travels with the payload because the arbitrage consumer cannot
    recover it later without going back to bronze per leg. Bronze writes a row
    only when a payload *changes*, so two books' "latest" rows can be minutes
    apart — and an arbitrage computed across legs of very different ages is an
    artefact, not an opportunity. `fact_arbitrage_signal` records that spread;
    this is where the inputs come from.
    """

    payload: bytes
    fire_time: datetime


_DSN_ENV_VAR = "MARKETS_DB_URL"

_LATEST_PER_BOOK = """
    SELECT DISTINCT ON (bookmaker) bookmaker, payload, fire_time
    FROM bronze_event_payloads
    WHERE event_id = %s
    ORDER BY bookmaker, write_time DESC
"""


def latest_payloads(conn: psycopg.Connection, event_id: UUID) -> dict[str, BookPayload]:
    """The most recent payload each bookmaker published for `event_id`.

    Keys are bronze's own bookmaker names, which the parsers register under
    unchanged — bronze sits downstream of arbibet-markets' alias mapping, so
    its spelling is the platform's canonical one. Payloads are the verbatim
    response bytes, undecoded: bronze's contract is byte fidelity, and a
    malformed body should fail in the parser, where that failure has a reason
    code, rather than here.

    Every book bronze holds is returned. Choosing which of them to parse is the
    caller's decision, not this function's.
    """
    with conn.cursor() as cur:
        cur.execute(_LATEST_PER_BOOK, (event_id,))
        return {book: BookPayload(payload, fire_time) for book, payload, fire_time in cur}


class HistoricPayload(NamedTuple):
    """One payload a bookmaker published for a fixture, at one moment."""

    bookmaker: str
    payload: bytes
    fire_time: datetime


# Same access path as the query above: `event_id` leads, and the index
# `idx_bep_event_book_write (event_id, bookmaker, write_time DESC)` covers the
# ordering too. Do NOT add a bookmaker-only filter (see the module docstring).
_HISTORY = """
    SELECT bookmaker, payload, fire_time
    FROM bronze_event_payloads
    WHERE event_id = %s
      AND (%s::timestamptz IS NULL OR fire_time > %s::timestamptz)
      AND (%s::timestamptz IS NULL OR fire_time <= %s::timestamptz)
    ORDER BY bookmaker, write_time
"""

# Each book's newest payload at or before a moment: the market as a replay
# resuming from that moment must start from, not from empty.
_AS_OF = """
    SELECT DISTINCT ON (bookmaker) bookmaker, payload, fire_time
    FROM bronze_event_payloads
    WHERE event_id = %s AND fire_time <= %s
    ORDER BY bookmaker, fire_time DESC, write_time DESC
"""


def payload_history(
    conn: psycopg.Connection,
    event_id: UUID,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[HistoricPayload]:
    """EVERY payload bronze holds for `event_id`, oldest first per book.

    `latest_payloads` answers "what is the price now", which is what the
    producer needs. This answers "how did the price get there", which is what a
    line-movement chart needs -- and bronze can answer it without any new
    collection, because it is append-only and writes a row on every change.
    That property makes it a tick store that nobody set out to build: the 11
    fixtures behind our arbitrage signals carry 4,984 payloads between them,
    150-230 per book, over about ten days.

    `since` filters on `fire_time`, the book's own clock and the column the
    tick store keys on -- so a caller can resume from the newest tick it
    already holds instead of replaying weeks of history it has already parsed.
    The filter is on top of an `event_id` equality, so the
    (event_id, bookmaker, write_time) index still drives the read.

    Two things the caller must know. Bronze prunes, so history reaches back
    only so far -- roughly seven weeks at the time of writing, and a fixture
    older than that returns nothing rather than an error. And a payload
    changing does NOT mean the market you care about changed: a row is written
    when ANY part of the book's response moves, so consecutive payloads
    routinely carry an identical price for a given outcome. Collapsing those is
    the caller's job, and `odds/ticks.py` does it.
    """
    with conn.cursor() as cur:
        cur.execute(_HISTORY, (event_id, since, since, until, until))
        return [HistoricPayload(book, payload, fire) for book, payload, fire in cur]


def payloads_as_of(conn: psycopg.Connection, event_id: UUID, at: datetime) -> list[HistoricPayload]:
    """Each book's newest payload for `event_id` at or before `at`."""
    with conn.cursor() as cur:
        cur.execute(_AS_OF, (event_id, at))
        return [HistoricPayload(book, payload, fire) for book, payload, fire in cur]


# `event_id = ANY(%s)` rather than a `write_time > cursor` scan. The watcher
# calls this every few seconds, and a bare write_time predicate has no index to
# stand on -- it would seq-scan the whole 6.9M-row table on every poll, the
# exact shape the module docstring warns against. Bounding it to the upcoming
# fixtures lets the (event_id, bookmaker, write_time) index serve it.
_LATEST_WRITE_TIMES = """
    SELECT event_id, max(write_time) AS write_time
    FROM bronze_event_payloads
    WHERE event_id = ANY(%s)
    GROUP BY event_id
"""


def latest_write_times(conn: psycopg.Connection, event_ids: list[UUID]) -> dict[UUID, datetime]:
    """The newest `write_time` bronze holds for each of `event_ids`.

    How the watcher asks "did anything land for these fixtures since I last
    looked". Returns only fixtures that have at least one payload, so a fixture
    bronze has never seen is simply absent rather than mapped to a sentinel.
    """
    if not event_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(_LATEST_WRITE_TIMES, (event_ids,))
        return {event_id: write_time for event_id, write_time in cur}


def connect() -> psycopg.Connection:
    """Read-only session against the markets bronze database."""
    return _connect(_DSN_ENV_VAR)
