import json
from datetime import UTC, datetime, timedelta

from arbibet_capstone.bronze import HistoricPayload
from arbibet_capstone.ticks import price_changes

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _payload(*prices: float) -> bytes:
    """A sportybet body quoting Over/Under 2.5 at the given prices."""
    return json.dumps(
        {
            "data": {
                "markets": [
                    {
                        "id": "18",
                        "specifier": "total=2.5",
                        "status": 0,
                        "desc": "Over/Under",
                        # sportybet is one of the two books that publish a
                        # probability, and its parser requires the field.
                        "outcomes": [
                            {
                                "id": str(12 + i),
                                # The book's own label, which the parser reads
                                # as the outcome name.
                                "desc": ("Over", "Under")[i],
                                "odds": str(p),
                                # A suspended outcome has no meaningful
                                # probability either, so 0 stands in.
                                "probability": str(round(1 / p, 4) if p else 0),
                                "isActive": 1,
                            }
                            for i, p in enumerate(prices)
                        ],
                    }
                ]
            }
        }
    ).encode()


def _history(*bodies: bytes) -> list[HistoricPayload]:
    return [
        HistoricPayload("sportybet", body, T0 + timedelta(minutes=i))
        for i, body in enumerate(bodies)
    ]


def _mappings() -> object:
    from arbibet_capstone.crosswalk.mappings import market_mappings

    return market_mappings()


def test_an_unchanged_price_is_not_a_tick() -> None:
    # Bronze writes a row whenever ANY part of a book's response moves, so
    # consecutive payloads routinely repeat the price of the outcome being
    # charted. Emitting one row per payload would multiply the table over and
    # draw a step chart whose steps are invisible.
    history = _history(_payload(1.90, 1.90), _payload(1.90, 1.90))

    result = price_changes(history, {"18"}, _mappings())

    assert len({(t.outcome_id, t.fire_time) for t in result.ticks}) == 2
    assert all(t.fire_time == T0 for t in result.ticks)


def test_a_moved_price_is_a_tick() -> None:
    history = _history(_payload(1.90, 1.90), _payload(2.05, 1.90))

    result = price_changes(history, {"18"}, _mappings())
    moved = [t for t in result.ticks if t.fire_time != T0]

    assert [t.odds for t in moved] == [2.05]


def test_the_first_observation_is_always_a_tick() -> None:
    # A price we have never seen is news even if the book had been quoting it
    # for hours. Suppressing it leaves a series that begins in mid-air.
    result = price_changes(_history(_payload(1.90, 1.90)), {"18"}, _mappings())

    assert len(result.ticks) == 2
    assert all(t.fire_time == T0 for t in result.ticks)


def test_markets_outside_the_requested_set_are_ignored() -> None:
    # The whole cost argument. Every market in every payload is a few hundred
    # thousand rows nobody reads; the markets a signal was raised on are a few
    # thousand that someone will interrogate.
    result = price_changes(_history(_payload(1.90, 1.90)), {"1"}, _mappings())

    assert result.ticks == []


def test_an_unreadable_payload_is_counted_rather_than_raised() -> None:
    # This replays weeks of history, where a body written under an older
    # response shape is an ordinary fact. Failing the backfill on one of them
    # would mean the chart can never be built -- but hiding it would let a
    # half-parsed backfill draw a confident, half-empty chart.
    history = [
        HistoricPayload("sportybet", b"{not json", T0),
        *_history(_payload(1.90, 1.90)),
    ]

    result = price_changes(history, {"18"}, _mappings())

    assert result.failed_payloads == 1
    assert len(result.ticks) == 2


def test_a_book_with_no_parser_is_skipped_silently() -> None:
    # Bronze holds fourteen books; five have parsers. That is a known scope
    # decision, not an error, and it must not inflate the failure count.
    history = [HistoricPayload("betking", b"{}", T0)]

    result = price_changes(history, {"18"}, _mappings())

    assert result.ticks == []
    assert result.failed_payloads == 0


def test_a_suspended_outcome_is_not_a_price() -> None:
    # One book emits 0.00 for a suspended outcome instead of omitting it.
    # Charted, that draws a price collapsing to zero and recovering -- a
    # suspension rendered as a market move. Decimal odds of 1.00 return the
    # stake and nothing else, so neither is a bet anyone can place.
    history = _history(_payload(0.0, 1.0))

    result = price_changes(history, {"18"}, _mappings())

    assert result.ticks == []
    assert result.failed_payloads == 0


def test_every_line_of_a_market_is_pulled_not_just_the_one_that_fired() -> None:
    # wanted_markets holds BASE ids ('18'), not full ones ('18;2.5'), so a
    # signal on Over/Under 2.5 brings 3.5 with it. That is what lets the chart
    # offer the lines side by side, and it costs almost nothing.
    body = json.dumps(
        {
            "data": {
                "markets": [
                    {
                        "id": "18",
                        "specifier": f"total={line}",
                        "status": 0,
                        "desc": "Over/Under",
                        "outcomes": [
                            {
                                "id": "12",
                                "odds": "1.90",
                                "probability": "0.52",
                                "isActive": 1,
                            }
                        ],
                    }
                    for line in ("2.5", "3.5")
                ]
            }
        }
    ).encode()

    result = price_changes(_history(body), {"18"}, _mappings())

    assert sorted(t.market_id for t in result.ticks) == ["18;2.5", "18;3.5"]


def test_the_books_name_for_an_outcome_is_carried_through() -> None:
    # The settlement taxonomy names outcomes only for markets the engine can
    # settle, which is why the surebets table displayed "outcome 12". The books
    # publish a name; keeping it is the only honest label for the rest.
    result = price_changes(_history(_payload(1.90, 2.10)), {"18"}, _mappings())

    assert {t.outcome_name for t in result.ticks} == {"Over", "Under"}


def test_a_seeded_resume_does_not_re_emit_an_unchanged_price() -> None:
    # The trap that makes an incremental run WRONG rather than merely partial.
    # price_changes emits when a price differs from the previous one it saw, so
    # a run resuming mid-history with no memory treats the first payload after
    # the cursor as a change and writes a phantom tick for a price that never
    # moved. Seeded with the last stored price, it emits nothing.
    resumed = _history(_payload(1.90, 2.10))
    seed = {
        ("sportybet", "18;2.5", "12"): 1.90,
        ("sportybet", "18;2.5", "13"): 2.10,
    }

    assert price_changes(resumed, {"18"}, _mappings(), seed).ticks == []
    # Unseeded, the same payload looks like two brand-new prices.
    assert len(price_changes(resumed, {"18"}, _mappings()).ticks) == 2


def test_a_seeded_resume_still_reports_a_real_move() -> None:
    # The other half: the seed must not suppress an actual change.
    moved = _history(_payload(2.05, 2.10))
    seed = {
        ("sportybet", "18;2.5", "12"): 1.90,
        ("sportybet", "18;2.5", "13"): 2.10,
    }

    ticks = price_changes(moved, {"18"}, _mappings(), seed).ticks

    assert [(t.outcome_id, t.odds) for t in ticks] == [("12", 2.05)]
