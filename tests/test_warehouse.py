import pytest

from arbibet_capstone.warehouse import _merge_into, _merge_sql

_COLS = ["signal_key", "event_id", "arbitrage", "legs"]


def test_key_columns_are_matched_on_and_never_updated() -> None:
    sql = _merge_sql("fact_arbitrage_signal", _COLS, ["signal_key"], ())

    assert "ON t.signal_key = s.signal_key " in sql
    assert "t.signal_key = s.signal_key," not in sql  # not in the UPDATE SET
    assert "UPDATE SET t.event_id = s.event_id, t.arbitrage = s.arbitrage" in sql


def test_a_composite_key_matches_on_every_part() -> None:
    # fact_team_market_result is keyed on five columns; an ON clause missing
    # one would merge rows that are not the same row.
    sql = _merge_sql("t", ["a", "b", "c"], ["a", "b"], ())

    assert "ON t.a = s.a AND t.b = s.b " in sql


def test_variant_columns_are_parsed_not_stored_as_text() -> None:
    # `legs` is VARIANT. A bare bind would store the JSON as a string, and
    # dbt could not read into it.
    sql = _merge_sql("fact_arbitrage_signal", _COLS, ["signal_key"], {"legs"})

    assert "PARSE_JSON(%s) AS legs" in sql
    assert "%s AS arbitrage" in sql  # everything else binds plainly


def test_a_key_that_is_not_a_column_is_refused() -> None:
    with pytest.raises(ValueError, match="not a subset"):
        _merge_sql("t", ["a", "b"], ["missing"], ())


def test_a_malformed_identifier_is_refused() -> None:
    # A typo trap, not an injection defence: these names come from our code.
    with pytest.raises(ValueError, match="not a valid SQL identifier"):
        _merge_sql("fact; DROP TABLE x", ["a"], ["a"], ())


def test_a_rolling_source_cannot_erase_what_the_warehouse_already_knows() -> None:
    # `apifootball_events` is a rolling window. A fixture that resolved to team
    # ids last week still has an `apifootball` leg in the matcher today, so the
    # LEFT JOIN still yields a row -- with NULL ids. A plain `t.c = s.c` wrote
    # those NULLs over ids dim_fixture already held, and every slip leg
    # pointing at that fixture silently lost its settled history.
    sql = _merge_into(
        "dim_fixture",
        ["event_id", "home_team_id", "kickoff_at"],
        ["event_id"],
        "stg",
        keep=["home_team_id"],
    )

    assert "t.home_team_id = COALESCE(s.home_team_id, t.home_team_id)" in sql
    # Kickoff genuinely changes: a postponement must win, so it is not kept.
    assert "t.kickoff_at = s.kickoff_at" in sql


def test_columns_are_overwritten_unless_the_caller_asks_otherwise() -> None:
    # The preserving behaviour is opt-in. A fact writer that means to set a
    # column back to NULL must still be able to.
    sql = _merge_into("f", ["k", "v"], ["k"], "stg")

    assert "t.v = s.v" in sql
    assert "COALESCE" not in sql
