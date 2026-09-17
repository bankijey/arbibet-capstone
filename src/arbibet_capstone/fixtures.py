"""The fixture list: which matches the producer should build snapshots for.

Source is arbibet-matcher's `event_matches` in the sources DB — one row per
real-world fixture, with every bookmaker's id for it in a JSONB array. Its `id`
is the same UUID that keys `bronze_event_payloads`, which is what makes the
producer's two reads line up.

Access path: the query filters on `(sport_key, start)`, which is exactly
`event_matches_sport_start_idx`. The JSONB array is unnested per row, not
searched across rows — the GIN index on `bookmaker_event_ids` exists for
containment queries this one deliberately does not do.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

import psycopg

from arbibet_capstone.db import connect as _connect

_DSN_ENV_VAR = "SOURCES_DB_URL"


class Fixture(NamedTuple):
    """One upcoming fixture, in both identity spaces the pipeline needs.

    `event_id` keys markets bronze. The API-Football ids key the historical
    facts: `fixture_id` identifies the match itself, and the two team ids are
    what `fact_team_match` and `fact_team_market_result` are keyed on, so a
    fixture can be joined to its sides' history without a second lookup.

    All four API-Football fields are optional. The matcher resolves an
    apifootball leg for most fixtures but not all, and a fixture without one
    is still perfectly priceable — it simply has no history to draw on. That
    is a quality gradient, not a failure, so it is carried as NULL rather than
    filtered out here.
    """

    event_id: UUID
    kickoff: datetime
    home_team: str | None
    away_team: str | None
    tournament: str | None
    apifootball_id: int | None
    home_team_id: int | None
    away_team_id: int | None
    # The betradar match id, e.g. "sr:match:73936890". Every book's e_id for a
    # fixture carries the SAME one -- `msports;sr:match:X`, `sportybet;sr:match:X`,
    # `ilobet;sr:match:X` -- so it is the fixture's identity in betradar space,
    # not a per-book id. It is the only key a booking slip can be joined on:
    # slips name fixtures by sr:match and teams by sr:competitor, neither of
    # which appears anywhere else in this warehouse.
    sr_match_id: str | None
    # What each of OUR books' listing names as home and away, as the matcher
    # recorded it: the fallback when a payload names no teams
    # (arbibet_capstone.verify). Defaulted so callers that build a Fixture by
    # hand (arbitrage_track) need not know about it.
    book_names: dict[str, tuple[str, str]] | None = None


# The lateral picks the apifootball leg out of the JSONB array; the LEFT JOIN
# then reaches the projection for the team ids, which live only there. Both are
# LEFT so a fixture without an apifootball leg survives with NULLs.
#
# Names are COALESCEd: the projection's spelling is API-Football's canonical
# one, which is what the historical facts and the LLM see, so it wins. The
# first array element is the fallback, because every element carries the names
# and a bookmaker's spelling beats no name at all.
_UPCOMING = """
    SELECT
        m.id,
        m.start,
        COALESCE(a.home_team,  m.bookmaker_event_ids->0->>'home_team'),
        COALESCE(a.away_team,  m.bookmaker_event_ids->0->>'away_team'),
        COALESCE(a.tournament, m.bookmaker_event_ids->0->>'tournament'),
        -- The id comes from the matcher's LEG, falling back to the
        -- projection, because the leg is durable and the projection is not:
        -- `apifootball_events` is a rolling window, and a fixture whose ids
        -- have aged out of it still carries `apifootball;<id>` in
        -- `bookmaker_event_ids`. Reading it from the leg keeps the one value
        -- that lets team ids be recovered downstream from `fact_team_match`.
        COALESCE(a.fixture_id, split_part(af.e_id, ';', 2)::bigint),
        a.home_id::bigint,
        a.away_id::bigint,
        sr.sr_match_id,
        m.bookmaker_event_ids
    FROM event_matches m
    LEFT JOIN LATERAL (
        SELECT e->>'e_id' AS e_id
        FROM jsonb_array_elements(m.bookmaker_event_ids) e
        WHERE e->>'e_id' LIKE 'apifootball;%%'
        LIMIT 1
    ) af ON TRUE
    LEFT JOIN apifootball_events a ON a.e_id = af.e_id
    LEFT JOIN LATERAL (
        SELECT split_part(e->>'e_id', ';', 2) AS sr_match_id
        FROM jsonb_array_elements(m.bookmaker_event_ids) e
        WHERE e->>'e_id' LIKE '%%;sr:match:%%'
        LIMIT 1
    ) sr ON TRUE
    WHERE m.sport_key = %s
      AND m.start >= %s
      AND m.start <  %s
    ORDER BY m.start
"""


def upcoming(
    conn: psycopg.Connection,
    *,
    since: datetime,
    until: datetime,
    sport_key: str = "football",
) -> list[Fixture]:
    """Fixtures kicking off in `[since, until)`, earliest first.

    The window is the caller's decision, not this function's: the producer
    wants matches bronze has recently polled, a backfill wants something else,
    and a default here would be policy hidden in a library.
    """
    with conn.cursor() as cur:
        cur.execute(_UPCOMING, (sport_key, since, until))
        return [
            Fixture(*row[:9], book_names=_book_names(row[9]) if len(row) > 9 else None)
            for row in cur
        ]


def _book_names(legs: Any) -> dict[str, tuple[str, str]]:
    """Our books' team names from the matcher's legs, keyed by OUR bookmaker names."""
    names: dict[str, tuple[str, str]] = {}
    for leg in legs or []:
        source = str(leg.get("e_id", "")).split(";", 1)[0]
        book = _LINK_BOOKS.get(source)
        home, away = leg.get("home_team"), leg.get("away_team")
        if book and home and away and book not in names:
            names[book] = (str(home), str(away))
    return names


# The matcher's source prefixes differ from our bookmaker names for two books.
# Only the five books this platform prices are linked.
_LINK_BOOKS = {
    "sportybet": "sportybet",
    "msports": "msport",
    "ilobet": "ilotbet",
    "bet9ja": "bet9ja",
    "livescorebet": "livescorebet",
}

# Each matched fixture's page on each book, from the collector's `all_events`.
#
# Tested before being published: one upcoming fixture opened on all five books
# and every link landed on that match's own page with live 1X2 prices. msport,
# sportybet and ilotbet URLs carry the sr:match id and bet9ja and livescorebet
# their own event ids -- the same shapes as every football link in the last 60
# days. msport sends a first-time visitor to its welcome page once; the second
# click lands on the match.
_LINKS = """
    SELECT m.id, split_part(e->>'e_id', ';', 1) AS source_book, a.url
    FROM event_matches m, jsonb_array_elements(m.bookmaker_event_ids) e
    JOIN all_events a ON a.e_id = e->>'e_id'
    WHERE m.sport_key = %s
      AND m.start >= %s
      AND m.start <  %s
      AND a.url IS NOT NULL
"""


class EventLink(NamedTuple):
    event_id: UUID
    bookmaker: str  # our name for the book, e.g. "msport"
    url: str


def event_links(
    conn: psycopg.Connection,
    *,
    since: datetime,
    until: datetime,
    sport_key: str = "football",
) -> list[EventLink]:
    """Every linked book page for fixtures kicking off in `[since, until)`."""
    with conn.cursor() as cur:
        cur.execute(_LINKS, (sport_key, since, until))
        return [
            EventLink(event_id, _LINK_BOOKS[book], url)
            for event_id, book, url in cur
            if book in _LINK_BOOKS
        ]


def connect() -> psycopg.Connection:
    """Read-only session against the sources database."""
    return _connect(_DSN_ENV_VAR)
