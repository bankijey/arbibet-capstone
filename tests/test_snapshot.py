import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from arbibet_capstone.bronze import BookPayload
from arbibet_capstone.fixtures import Fixture
from arbibet_capstone.snapshot import build

_T0 = datetime(2026, 9, 2, 18, 30, tzinfo=UTC)
_T1 = datetime(2026, 9, 2, 18, 10, tzinfo=UTC)

_FIXTURE = Fixture(
    event_id=uuid4(),
    kickoff=datetime(2026, 9, 2, 19, 0, tzinfo=UTC),
    home_team="Man City",
    away_team="Porto",
    tournament="UCL",
    apifootball_id=1499622,
    home_team_id=50,
    away_team_id=212,
    sr_match_id="sr:match:1",
)

# A betradar-native book: market id 1 (1X2) with two priced outcomes. Minimal,
# but shaped exactly as msport publishes -- specifiers empty, isActive flags,
# odds and probability as strings.
_MSPORT = (
    b'{"data":{"markets":[{"id":"1","specifiers":"","desc":"1x2","outcomes":['
    b'{"id":"1","isActive":1,"odds":"2.10","probability":"0.45","description":"Home"},'
    b'{"id":"3","isActive":1,"odds":"3.40","probability":"0.28","description":"Away"}'
    b"]}]}}"
)

_SPORTYBET = (
    b'{"data":{"markets":[{"id":"1","specifier":"","desc":"1x2","outcomes":['
    b'{"id":"1","isActive":1,"odds":"2.05","probability":"0.46","desc":"Home"},'
    b'{"id":"3","isActive":1,"odds":"3.60","probability":"0.27","desc":"Away"}'
    b"]}]}}"
)


def test_two_books_land_on_the_same_canonical_market_id() -> None:
    # The whole point of the platform: after build(), the same market at two
    # books carries the same key, so a cross-book comparison is a dict lookup.
    snap = build(
        _FIXTURE,
        {"msport": BookPayload(_MSPORT, _T0), "sportybet": BookPayload(_SPORTYBET, _T1)},
    )

    assert set(snap.books) == {"msport", "sportybet"}
    assert [m.market_id for m in snap.books["msport"].markets] == ["1"]
    assert [m.market_id for m in snap.books["sportybet"].markets] == ["1"]


def test_each_book_keeps_its_own_fire_time() -> None:
    # Bronze writes only on change, so books' latest rows differ in age. That
    # spread is what separates a real arbitrage from an artefact, and it is
    # only recoverable if the times survive parsing.
    snap = build(
        _FIXTURE,
        {"msport": BookPayload(_MSPORT, _T0), "sportybet": BookPayload(_SPORTYBET, _T1)},
    )

    assert snap.books["msport"].fire_time == _T0
    assert snap.books["sportybet"].fire_time == _T1


def test_books_without_a_parser_are_skipped_not_attempted() -> None:
    # Bronze holds fourteen books; five have parsers. The rest are a normal
    # part of every payload set, not an error condition.
    snap = build(
        _FIXTURE,
        {"msport": BookPayload(_MSPORT, _T0), "betking": BookPayload(b"{}", _T0)},
    )

    assert set(snap.books) == {"msport"}


def test_a_book_that_parses_to_nothing_is_left_out() -> None:
    empty = b'{"data":{"markets":[]}}'
    snap = build(_FIXTURE, {"msport": BookPayload(empty, _T0)})

    assert snap.books == {}
    assert snap.fixture == _FIXTURE


def test_a_malformed_payload_raises_rather_than_vanishing() -> None:
    # The defect this pipeline exists to avoid: a book that silently yields no
    # markets is indistinguishable from a book that had none. The specific
    # exception matters -- a bare `Exception` would pass even if this failed
    # for an unrelated reason.
    with pytest.raises(json.JSONDecodeError):
        build(_FIXTURE, {"msport": BookPayload(b"not json", _T0)})


def test_the_wire_format_round_trips() -> None:
    # The producer writes this and both consumers read it. A round trip is the
    # only test that keeps the two halves honest, and it is only possible
    # because there is one definition rather than a writer and a reader.
    from arbibet_capstone.snapshot import from_wire, to_wire

    original = build(
        _FIXTURE,
        {"msport": BookPayload(_MSPORT, _T0), "sportybet": BookPayload(_SPORTYBET, _T1)},
    )

    restored = from_wire(json.loads(json.dumps(to_wire(original))))

    assert restored.fixture == original.fixture
    assert set(restored.books) == set(original.books)
    for book, quote in original.books.items():
        assert restored.books[book].fire_time == quote.fire_time
        assert restored.books[book].markets == quote.markets
