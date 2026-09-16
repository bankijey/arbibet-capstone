from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from arbibet_capstone import arbitrage_track
from arbibet_capstone.arbitrage_track import replay
from arbibet_capstone.bronze import HistoricPayload
from arbibet_capstone.crosswalk.models import Market, Outcome
from arbibet_capstone.fixtures import Fixture

T0 = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
FIXTURE = Fixture(uuid4(), T0 + timedelta(hours=6), None, None, None, None, None, None, None)


def _market(home: float, draw: float, away: float) -> list[Market]:
    return [
        Market(
            market_id="1",
            market_name="1X2",
            outcomes=[
                Outcome(id="1", name="Home", odds=home, p=0.45),
                Outcome(id="2", name="Draw", odds=draw, p=0.27),
                Outcome(id="3", name="Away", odds=away, p=0.28),
            ],
        )
    ]


@pytest.fixture
def books(monkeypatch: pytest.MonkeyPatch) -> dict[bytes, list[Market]]:
    """Payload bodies are opaque here: each maps straight to parsed markets."""
    table: dict[bytes, list[Market]] = {}
    # The module's own `json` name is replaced, not json.loads itself -- that
    # would break every other json user in the process for the test's length.
    monkeypatch.setattr(arbitrage_track, "json", SimpleNamespace(loads=lambda raw: raw))
    monkeypatch.setattr(arbitrage_track, "parse_bookmaker", lambda _book, body, _m: table[body])
    return table


def _payload(
    table: dict[bytes, list[Market]], book: str, minute: float, markets: list[Market]
) -> HistoricPayload:
    body = f"{book}-{minute}".encode()
    table[body] = markets
    return HistoricPayload(book, body, T0 + timedelta(minutes=minute))


def test_a_surebet_opens_closes_and_is_recorded_below_one(books) -> None:
    history = [
        _payload(books, "msport", 0, _market(3.20, 3.00, 2.20)),
        _payload(books, "sportybet", 1, _market(2.20, 3.10, 2.60)),   # below 1
        _payload(books, "sportybet", 2, _market(2.20, 4.00, 3.50)),   # 1.179 surebet
        _payload(books, "msport", 3, _market(2.00, 3.00, 2.20)),      # closes
    ]

    track = replay(FIXTURE, history, {"1"}, mappings=None)

    values = [p.arbitrage for p in track.points]
    assert len(values) == 3
    assert values[0] < 1 < values[1] and values[2] < 1


def test_a_stale_leg_is_marked_not_dropped(books) -> None:
    history = [
        _payload(books, "msport", 0, _market(3.20, 3.00, 2.20)),
        _payload(books, "sportybet", 60, _market(2.20, 4.00, 3.50)),  # an hour later
    ]
    (point,) = replay(FIXTURE, history, {"1"}, mappings=None).points
    assert point.arbitrage > 1 and point.leg_spread_seconds == 3600


def test_a_book_dropping_the_market_ends_the_cross_book_price(books) -> None:
    history = [
        _payload(books, "msport", 0, _market(3.20, 3.00, 2.20)),
        _payload(books, "sportybet", 1, _market(2.20, 4.00, 3.50)),
        _payload(books, "sportybet", 2, []),  # sportybet stops pricing it
    ]
    points = replay(FIXTURE, history, {"1"}, mappings=None).points
    assert points[-1].arbitrage is None


def test_a_resumed_replay_does_not_re_emit_an_unchanged_state(books) -> None:
    seed = [
        _payload(books, "msport", 0, _market(3.20, 3.00, 2.20)),
        _payload(books, "sportybet", 1, _market(2.20, 4.00, 3.50)),
    ]
    full = replay(FIXTURE, seed, {"1"}, mappings=None)
    last = full.points[-1]

    # msport re-fires with the same prices; nothing about the market changed.
    later = [_payload(books, "msport", 2, _market(3.20, 3.00, 2.20))]
    resumed = replay(
        FIXTURE, later, {"1"}, mappings=None,
        seed_payloads=seed,
        seed_state={"1": (last.arbitrage, True)},
    )
    assert resumed.points == []
