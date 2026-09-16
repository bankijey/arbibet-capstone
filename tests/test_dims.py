from datetime import UTC, datetime
from uuid import uuid4

from arbibet_capstone.dims import ARB_RELEVANT_MARKETS, fixture_rows, market_rows
from arbibet_capstone.fixtures import Fixture


def test_the_crosswalk_becomes_one_row_per_market() -> None:
    rows = market_rows()
    ids = [r["market_base_id"] for r in rows]

    assert len(ids) == len(set(ids)), "dim_market must be unique on market_base_id"
    assert len(rows) > 200


def test_the_duplicate_market_appears_once() -> None:
    # The CSV carries id 142 twice, once per bet9ja key, because one betradar
    # market can have more than one name at a book. It is still one market, and
    # a second row would fail the dbt uniqueness test.
    rows = [r for r in market_rows() if r["market_base_id"] == 142]

    assert len(rows) == 1


def test_the_mainstream_markets_are_flagged_arb_relevant() -> None:
    by_id = {r["market_base_id"]: r for r in market_rows()}

    for market in (1, 10, 11, 18):  # 1X2, double chance, DNB, over/under
        assert by_id[market]["is_arb_relevant"] is True
    assert by_id[45]["is_arb_relevant"] is False  # correct score: too many outcomes


def test_absent_crosswalk_entries_become_null_not_nan() -> None:
    # pandas leaves NaN in empty cells, which is neither None nor falsy and
    # would reach Snowflake as a float where an integer column is declared.
    rows = market_rows()

    for row in rows:
        assert row["livescorebet_type"] is None or isinstance(row["livescorebet_type"], int)
        assert row["bet9ja_key"] is None or isinstance(row["bet9ja_key"], str)


def test_a_fixture_row_carries_both_identity_spaces() -> None:
    event_id = uuid4()
    kickoff = datetime(2026, 9, 2, 19, 0, tzinfo=UTC)
    (row,) = fixture_rows(
        [
            Fixture(
                event_id=event_id,
                kickoff=kickoff,
                home_team="Burnley",
                away_team="Middlesbrough",
                tournament="Championship",
                apifootball_id=1499622,
                home_team_id=44,
                away_team_id=55,
                sr_match_id="sr:match:1",
            )
        ]
    )

    assert row["event_id"] == str(event_id)
    assert row["apifootball_id"] == 1499622
    assert (row["home_team_id"], row["away_team_id"]) == (44, 55)


def test_the_arb_relevant_set_is_declared_not_empty() -> None:
    assert len(ARB_RELEVANT_MARKETS) > 20
