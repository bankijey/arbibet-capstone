from datetime import UTC, datetime, timedelta
from uuid import uuid4

from arbibet_capstone.crosswalk.models import Market, Outcome
from arbibet_capstone.fixtures import Fixture
from arbibet_capstone.signals import _signal_key, _split_market, arbitrage_rows
from arbibet_capstone.snapshot import BookQuote, Snapshot

_NEW = datetime(2026, 9, 2, 18, 30, tzinfo=UTC)
_OLD = datetime(2026, 9, 2, 18, 25, 38, tzinfo=UTC)  # 262s earlier

_FIXTURE = Fixture(
    event_id=uuid4(),
    kickoff=datetime(2026, 9, 2, 19, 0, tzinfo=UTC),
    home_team="Burnley",
    away_team="Middlesbrough",
    tournament="Championship",
    apifootball_id=1,
    home_team_id=44,
    away_team_id=55,
                sr_match_id="sr:match:1",
)


def _market(home: float, draw: float, away: float) -> list[Market]:
    """A real 1X2: betradar market 1, outcomes 1 / 2 / 3.

    Three outcomes, not two. An earlier version of this fixture had only home
    and away because that made the arbitrage arithmetic trivial -- but market 1
    HAS a draw, and a two-outcome "1X2" is a fixture that teaches a reader
    something false. It also meant nothing here exercised
    `assign_unique_bookmakers_for_market`, whose whole job is reassigning a
    book that wins more than one outcome, which cannot happen with two
    outcomes and two books.
    """
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


def _snapshot() -> Snapshot:
    # Best prices across the two books: home 3.20 at msport, draw 4.00 and
    # away 3.50 at sportybet. 1/3.20 + 1/4.00 + 1/3.50 = 0.848, so the set
    # returns 1.179 -- a real three-way surebet.
    return Snapshot(
        fixture=_FIXTURE,
        books={
            "msport": BookQuote(_market(3.20, 3.00, 2.20), _NEW),
            "sportybet": BookQuote(_market(2.20, 4.00, 3.50), _OLD),
        },
    )


def test_a_market_id_splits_on_the_first_separator_only() -> None:
    # bet9ja writes market 14's specifier as "1:0"; a naive split loses half.
    assert _split_market("18;2.5") == (18, "2.5")
    assert _split_market("1") == (1, None)
    assert _split_market("14;1:0") == (14, "1:0")


def test_a_surebet_becomes_one_row_with_its_legs() -> None:
    (row,) = arbitrage_rows(_snapshot())

    assert row["market_base_id"] == 1
    assert row["specifier"] is None
    assert row["arbitrage"] > 1.0
    assert row["n_legs"] == 3
    assert {leg["outcome_id"] for leg in row["legs"]} == {"1", "2", "3"}


def test_leg_spread_comes_from_the_books_that_were_actually_used() -> None:
    # The freshness columns are the difference between a tradeable signal and
    # an artefact of stale legs.
    (row,) = arbitrage_rows(_snapshot())

    assert row["oldest_leg_fire_time"] == _OLD
    assert row["newest_leg_fire_time"] == _NEW
    assert row["leg_spread_seconds"] == 262


def test_the_same_snapshot_produces_the_same_key_twice() -> None:
    # This is what makes the producer re-runnable: reprocessing a message
    # merges onto the same row instead of duplicating it.
    first = arbitrage_rows(_snapshot())[0]["signal_key"]
    second = arbitrage_rows(_snapshot())[0]["signal_key"]

    assert first == second


def test_a_different_price_picture_is_a_different_signal() -> None:
    changed = Snapshot(
        fixture=_FIXTURE,
        books={
            "msport": BookQuote(_market(3.30, 3.00, 2.20), _NEW),
            "sportybet": BookQuote(_market(2.20, 4.00, 3.50), _OLD),
        },
    )

    assert arbitrage_rows(_snapshot())[0]["signal_key"] != arbitrage_rows(changed)[0]["signal_key"]


def test_leg_order_does_not_change_the_key() -> None:
    legs = [
        {"outcome_id": "1", "bookmaker": "msport", "odds": 2.6},
        {"outcome_id": "3", "bookmaker": "sportybet", "odds": 2.55},
    ]
    assert _signal_key("e", "1", legs) == _signal_key("e", "1", list(reversed(legs)))


def test_one_book_cannot_produce_an_arbitrage() -> None:
    single = Snapshot(
        fixture=_FIXTURE, books={"msport": BookQuote(_market(3.20, 3.00, 2.20), _NEW)}
    )

    assert arbitrage_rows(single) == []


def _ev_snapshot() -> Snapshot:
    """sportybet publishes a probability; bet9ja prices the same outcome longer."""
    priced = [
        Market(
            market_id="1",
            market_name="1X2",
            outcomes=[
                Outcome(id="1", name="Home", odds=1.90, p=0.60),
                Outcome(id="2", name="Draw", odds=3.60, p=0.27),
                Outcome(id="3", name="Away", odds=4.00, p=0.25),
            ],
        )
    ]
    generous = [
        Market(
            market_id="1",
            market_name="1X2",
            outcomes=[
                Outcome(id="1", name="Home", odds=2.10, p=None),
                Outcome(id="2", name="Draw", odds=3.50, p=None),
                Outcome(id="3", name="Away", odds=4.10, p=None),
            ],
        )
    ]
    return Snapshot(
        fixture=_FIXTURE,
        books={
            "sportybet": BookQuote(priced, _NEW),
            "bet9ja": BookQuote(generous, _OLD),
        },
    )


def test_ev_is_computed_against_a_borrowed_probability() -> None:
    from arbibet_capstone.signals import ev_rows

    rows = ev_rows(_ev_snapshot())

    # 0.60 * 2.10 - 1 = 0.26 at bet9ja; 0.60 * 1.90 - 1 = 0.14 at sportybet.
    by_book = {r["bookmaker"]: r for r in rows if r["outcome_id"] == "1"}
    assert round(by_book["bet9ja"]["ev"], 4) == 0.26
    assert round(by_book["sportybet"]["ev"], 4) == 0.14


def test_every_row_records_which_book_lent_the_probability() -> None:
    # An EV number with no provenance cannot be audited later.
    from arbibet_capstone.signals import ev_rows

    assert {r["p_source"] for r in ev_rows(_ev_snapshot())} == {"sportybet"}


def test_longshots_are_excluded_by_the_probability_floor() -> None:
    # The Away outcome at p=0.25 clears the EV bar (0.25*4.10-1 = 0.025) but
    # not the probability floor, where a small estimate error swamps the edge.
    from arbibet_capstone.signals import ev_rows

    assert {r["outcome_id"] for r in ev_rows(_ev_snapshot())} == {"1"}


def test_a_suspended_price_is_not_a_free_bet() -> None:
    from arbibet_capstone.signals import ev_rows

    # One outcome, on purpose. A PARSED market can legitimately be partial:
    # the parsers drop outcomes a book has flagged inactive, so a 1X2 whose
    # draw is suspended really does arrive with two. This one is down to a
    # single outcome priced at zero, which is what the test is about.
    suspended = Snapshot(
        fixture=_FIXTURE,
        books={
            "sportybet": BookQuote(
                [
                    Market(
                        market_id="1",
                        market_name="1X2",
                        outcomes=[Outcome(id="1", name="Home", odds=0.0, p=0.60)],
                    )
                ],
                _NEW,
            )
        },
    )

    assert ev_rows(suspended) == []


def test_a_book_may_hold_two_legs_when_it_beats_the_rest_twice() -> None:
    """Fewer books than outcomes means the legs cannot all differ.

    `assign_unique_bookmakers_for_market` spreads legs across books to maximise
    what is actually placeable, but with two books and three outcomes one of
    them must take two. Its documented fallback keeps the original assignment
    rather than dropping the outcome, so the "unique" in its name does not hold
    here -- and a reader who assumed otherwise would mis-read every three-way
    row in the table.

    Two bets at one bookmaker are still placeable, so this is a limit on the
    optimisation, not on the signal.
    """
    (row,) = arbitrage_rows(_snapshot())

    books = [leg["bookmaker"] for leg in row["legs"]]

    assert len(books) == 3
    assert len(set(books)) == 2


def test_a_market_priced_by_one_book_is_not_an_arbitrage() -> None:
    # The snapshot has two books, but THIS market is priced by only one of
    # them, so the engine builds every leg from that book. Two outcomes of one
    # book are that book's own overround, not a cross-book disagreement -- and
    # at 0.98 the near-arbitrage threshold happily recorded it. sportybet alone
    # on market 60210: 1.15 and 7.10, "arbitrage 0.9897", meaningless, and an
    # orphan against dim_market that failed the DAG's dbt tests.
    only_sportybet = [
        Market(
            market_id="60210",
            market_name="two-way",
            outcomes=[Outcome(id="1", odds=1.15), Outcome(id="2", odds=7.10)],
        )
    ]
    snapshot = Snapshot(
        fixture=_snapshot().fixture,
        books={
            "sportybet": BookQuote(only_sportybet, _NEW),
            "msport": BookQuote(_market(3.20, 3.00, 2.20), _NEW),
        },
    )

    rows = arbitrage_rows(snapshot, threshold=0.5)

    assert all(r["market_base_id"] != 60210 for r in rows)
    assert all(len({leg["bookmaker"] for leg in r["legs"]}) >= 2 for r in rows)


def test_legs_further_apart_than_the_freshness_bar_are_not_an_arbitrage() -> None:
    # The worst signal ever recorded here read 1.6788 -- a 68% guaranteed
    # return -- from a first-half 1X2 whose three prices were 78 minutes apart
    # and whose implied probabilities summed to 0.596. Every price was real;
    # they were never on sale together. FINDINGS 13g.
    stale = Snapshot(
        fixture=_FIXTURE,
        books={
            "msport": BookQuote(_market(3.20, 3.00, 2.20), _NEW),
            "sportybet": BookQuote(_market(2.20, 4.00, 3.50), _NEW - timedelta(minutes=78)),
        },
    )

    assert arbitrage_rows(stale) == []
    # The bar is a parameter, not a hidden constant: widen it and the same
    # snapshot is recorded again, spread and all.
    (row,) = arbitrage_rows(stale, max_leg_spread_seconds=78 * 60)
    assert row["leg_spread_seconds"] == 78 * 60


def test_detected_at_is_market_time_not_consumer_time() -> None:
    # detected_at once defaulted to the warehouse clock at MERGE. A snapshot
    # priced before kick-off and consumed after it then read as an in-play
    # detection, which is how 75 pre-kick-off surebets were mislabelled.
    (row,) = arbitrage_rows(_snapshot())

    assert row["detected_at"] == row["newest_leg_fire_time"] == _NEW


def _two_probability_books(
    sportybet_at: datetime, msport_at: datetime, bet9ja_at: datetime
) -> Snapshot:
    """sportybet says p=0.60, msport says p=0.55; bet9ja prices Home at 2.10."""

    def book(p: float | None, home: float) -> list[Market]:
        return [
            Market(
                market_id="1",
                market_name="1X2",
                outcomes=[Outcome(id="1", name="Home", odds=home, p=p)],
            )
        ]

    return Snapshot(
        fixture=_FIXTURE,
        books={
            "sportybet": BookQuote(book(0.60, 1.60), sportybet_at),
            "msport": BookQuote(book(0.55, 1.65), msport_at),
            "bet9ja": BookQuote(book(None, 2.10), bet9ja_at),
        },
    )


def _bet9ja_home(snapshot: Snapshot) -> dict[str, object]:
    from arbibet_capstone.signals import ev_rows

    (row,) = (r for r in ev_rows(snapshot) if r["bookmaker"] == "bet9ja")
    return row


def test_the_most_recently_published_probability_is_used() -> None:
    # msport published two minutes after sportybet. The old fixed priority
    # would have used sportybet's older 0.60 anyway.
    row = _bet9ja_home(
        _two_probability_books(_NEW - timedelta(minutes=2), _NEW, _NEW)
    )

    assert row["p_source"] == "msport"
    assert round(float(row["ev"]), 4) == round(0.55 * 2.10 - 1, 4)


def test_a_tie_on_publish_time_goes_to_sportybet() -> None:
    row = _bet9ja_home(_two_probability_books(_NEW, _NEW, _NEW))

    assert row["p_source"] == "sportybet"


def test_a_probability_too_old_for_the_price_is_refused() -> None:
    from arbibet_capstone.signals import ev_rows

    # Both probabilities are twenty minutes older than bet9ja's price. The
    # price may be current; what it is being judged against may not be.
    stale = _two_probability_books(
        _NEW - timedelta(minutes=20), _NEW - timedelta(minutes=20), _NEW
    )

    assert [r for r in ev_rows(stale) if r["bookmaker"] == "bet9ja"] == []


def test_ev_records_the_probability_age_and_market_time() -> None:
    row = _bet9ja_home(
        _two_probability_books(_NEW - timedelta(minutes=3), _NEW - timedelta(minutes=4), _NEW)
    )

    assert row["probability_fire_time"] == _NEW - timedelta(minutes=3)
    assert row["probability_spread_seconds"] == 180
    assert row["detected_at"] == _NEW
