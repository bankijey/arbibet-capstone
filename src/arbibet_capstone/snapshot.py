"""One fixture, one moment, every book — in a single market/outcome id space.

This is the unit the whole pipeline turns on. Arbitrage is a property of a
*set* of prices observed together, so the fixture snapshot, not the individual
quote, is what gets published and what gets consumed.

Three books (sportybet, msport, ilotbet) publish betradar market and outcome
ids natively. Two (bet9ja, livescorebet) publish proprietary schemes and are
translated by the crosswalk. After `build()` they are indistinguishable: every
market is `<betradar id>` or `<betradar id>;<specifier>`, and the same market
at two books carries the same key. Nothing downstream needs to know which
books needed translating.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

import psycopg

from arbibet_capstone.bronze import BookPayload, latest_payloads
from arbibet_capstone.crosswalk.mappings import market_mappings
from arbibet_capstone.crosswalk.models import Market
from arbibet_capstone.crosswalk.parsers import PARSER_REGISTRY, parse_bookmaker
from arbibet_capstone.fixtures import Fixture


class BookQuote(NamedTuple):
    """One book's markets for a fixture, and when that price picture was fired."""

    markets: list[Market]
    fire_time: datetime


class Snapshot(NamedTuple):
    """A fixture priced by N books, all speaking the same market ids.

    `books` is empty when nothing parsed — a real outcome for a fixture bronze
    has not polled yet, and one the caller decides what to do about. Returning
    an empty snapshot rather than None keeps the fixture's identity attached to
    that fact.
    """

    fixture: Fixture
    books: dict[str, BookQuote]


def build(fixture: Fixture, payloads: dict[str, BookPayload]) -> Snapshot:
    """Parse each book's payload into canonical markets.

    Only books with a registered parser are attempted. Bronze holds fourteen;
    five have parsers, and filtering here makes that a visible decision rather
    than nine "no parser registered" warnings per fixture.

    Parser failures are NOT caught. A payload that will not parse is a real
    defect — a book changed its shape, or the crosswalk is wrong — and the one
    thing this pipeline must never do is make that indistinguishable from a
    book having no markets. Isolation belongs in the producer loop, where a bad
    fixture can be skipped without hiding why.
    """
    mappings = market_mappings()
    books: dict[str, BookQuote] = {}

    for bookmaker, payload in payloads.items():
        if bookmaker not in PARSER_REGISTRY:
            continue
        markets = parse_bookmaker(bookmaker, json.loads(payload.payload), mappings)
        if markets:
            books[bookmaker] = BookQuote(markets, payload.fire_time)

    return Snapshot(fixture=fixture, books=books)


def fetch(conn: psycopg.Connection, fixture: Fixture) -> Snapshot:
    """Read the fixture's latest payloads from bronze and build its snapshot."""
    return build(fixture, latest_payloads(conn, fixture.event_id))


# --- wire format -------------------------------------------------------------
#
# The contract between the producer and both consumers. It lives here, beside
# the type it encodes, so there is exactly one definition of the shape -- a
# producer-side writer and a consumer-side reader would eventually disagree,
# and the round-trip test below is only possible because they cannot.
#
# Times are ISO-8601 strings and the event id is its canonical string form:
# JSON has neither type, and inventing an encoding for them would be a second
# thing to keep in step.


def to_wire(snapshot: Snapshot) -> dict[str, Any]:
    """Encode a snapshot as the JSON-ready dict published to `market.ticks`."""
    f = snapshot.fixture
    return {
        "event_id": str(f.event_id),
        "kickoff": f.kickoff.isoformat(),
        "home_team": f.home_team,
        "away_team": f.away_team,
        "tournament": f.tournament,
        "apifootball_id": f.apifootball_id,
        "home_team_id": f.home_team_id,
        "away_team_id": f.away_team_id,
        "sr_match_id": f.sr_match_id,
        "books": {
            book: {
                "fire_time": quote.fire_time.isoformat(),
                "markets": [m.model_dump() for m in quote.markets],
            }
            for book, quote in snapshot.books.items()
        },
    }


def from_wire(message: dict[str, Any]) -> Snapshot:
    """Decode a `market.ticks` message back into a snapshot."""
    fixture = Fixture(
        event_id=UUID(message["event_id"]),
        kickoff=datetime.fromisoformat(message["kickoff"]),
        home_team=message["home_team"],
        away_team=message["away_team"],
        tournament=message["tournament"],
        apifootball_id=message["apifootball_id"],
        home_team_id=message["home_team_id"],
        away_team_id=message["away_team_id"],
        sr_match_id=message.get("sr_match_id"),
    )
    return Snapshot(
        fixture=fixture,
        books={
            book: BookQuote(
                markets=[Market.model_validate(m) for m in body["markets"]],
                fire_time=datetime.fromisoformat(body["fire_time"]),
            )
            for book, body in message["books"].items()
        },
    )
