from datetime import UTC, datetime, timedelta

import pytest

from arbibet_capstone import warehouse
from arbibet_capstone.warehouse import _merge_into, _merge_sql

_COLS = ["signal_key", "event_id", "arbitrage", "legs"]


# --- the SQL ---------------------------------------------------------------------


def test_key_columns_are_matched_on_and_never_updated() -> None:
    sql = _merge_sql("fact_arbitrage_signal", _COLS, ["signal_key"], ())

    assert "ON t.signal_key = s.signal_key " in sql
    assert "signal_key = s.signal_key," not in sql  # not in the UPDATE SET
    assert "UPDATE SET event_id = s.event_id, arbitrage = s.arbitrage" in sql


def test_a_composite_key_matches_on_every_part() -> None:
    # fact_team_market_result is keyed on five columns; an ON clause missing
    # one would merge rows that are not the same row.
    sql = _merge_sql("t", ["a", "b", "c"], ["a", "b"], ())

    assert "ON t.a = s.a AND t.b = s.b " in sql


def test_json_columns_are_cast_not_stored_as_text() -> None:
    # `legs` is JSON. A bare bind would store a string, and dbt could not
    # unnest it.
    sql = _merge_sql("fact_arbitrage_signal", _COLS, ["signal_key"], {"legs"})

    assert "CAST(%s AS JSON) AS legs" in sql
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
    # LEFT JOIN still yields a row -- with NULL ids. A plain `c = s.c` wrote
    # those NULLs over ids dim_fixture already held, and every slip leg
    # pointing at that fixture silently lost its settled history.
    sql = _merge_into(
        "dim_fixture",
        ["event_id", "home_team_id", "kickoff_at"],
        ["event_id"],
        "stg",
        keep=["home_team_id"],
    )

    assert "home_team_id = COALESCE(s.home_team_id, t.home_team_id)" in sql
    # Kickoff genuinely changes: a postponement must win, so it is not kept.
    assert "kickoff_at = s.kickoff_at" in sql


def test_columns_are_overwritten_unless_the_caller_asks_otherwise() -> None:
    sql = _merge_into("f", ["k", "v"], ["k"], "stg")

    assert "v = s.v" in sql
    assert "COALESCE" not in sql


# --- against a real DuckDB --------------------------------------------------------


@pytest.fixture
def wh(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKDB_PATH", str(tmp_path / "test.duckdb"))
    monkeypatch.setenv("LAKE_PATH", str(tmp_path / "lake"))
    monkeypatch.setattr(warehouse, "_database", None)
    yield warehouse.connect()
    warehouse._database.close()
    monkeypatch.setattr(warehouse, "_database", None)


def test_the_schema_is_applied_and_seeded_once(wh) -> None:
    warehouse.connect()  # a second connect must not re-seed
    assert warehouse.bookmaker_ids(wh) == {
        "sportybet": 1,
        "msport": 2,
        "ilotbet": 3,
        "bet9ja": 4,
        "livescorebet": 5,
    }


def test_merge_inserts_then_updates_in_place(wh) -> None:
    now = datetime.now(UTC)
    row = {"scope": "ticks", "key": "e1", "position": now}
    warehouse.merge_bulk(wh, table="pipeline_cursor", rows=[row], key=["scope", "key"])
    later = now + timedelta(minutes=5)
    warehouse.merge_bulk(
        wh, table="pipeline_cursor", rows=[{**row, "position": later}], key=["scope", "key"]
    )

    frame = wh.query("SELECT key, position FROM core.pipeline_cursor")
    assert len(frame) == 1
    assert frame.POSITION[0] == later


def test_a_key_repeated_in_one_batch_keeps_the_last_row(wh) -> None:
    # DuckDB refuses a MERGE whose source matches one target row twice.
    now = datetime.now(UTC)
    rows = [
        {"scope": "s", "key": "k", "position": now},
        {"scope": "s", "key": "k", "position": now + timedelta(hours=1)},
    ]
    assert warehouse.merge_bulk(wh, table="pipeline_cursor", rows=rows, key=["scope", "key"]) == 1
    assert wh.query("SELECT position FROM core.pipeline_cursor").POSITION[0] == rows[1]["position"]


def test_a_null_from_a_rolling_source_does_not_overwrite(wh) -> None:
    now = datetime.now(UTC)
    base = {"event_id": "e1", "kickoff_at": now, "home_team_id": 42}
    warehouse.merge_bulk(wh, table="dim_fixture", rows=[base], key=["event_id"])
    warehouse.merge_bulk(
        wh,
        table="dim_fixture",
        rows=[{**base, "home_team_id": None}],
        key=["event_id"],
        keep=["home_team_id"],
    )
    assert wh.query("SELECT home_team_id FROM core.dim_fixture").HOME_TEAM_ID[0] == 42


def test_json_round_trips_as_json(wh) -> None:
    row = {
        "signal_key": "k",
        "event_id": "e",
        "market_id": "18;2.5",
        "market_base_id": 18,
        "arbitrage": 1.01,
        "n_legs": 2,
        "legs": [{"outcome_id": "12", "bookmaker": "msport", "odds": 2.1}],
        "oldest_leg_fire_time": datetime.now(UTC),
        "newest_leg_fire_time": datetime.now(UTC),
        "leg_spread_seconds": 3,
    }
    warehouse.merge(
        wh, table="fact_arbitrage_signal", rows=[row], key=["signal_key"], json_columns={"legs"}
    )
    got = wh.query(
        "SELECT json_type(legs) AS kind, legs->'$[0]'->>'bookmaker' AS book "
        "FROM core.fact_arbitrage_signal"
    )
    assert (got.KIND[0], got.BOOK[0]) == ("ARRAY", "msport")


def test_slip_versions_append_to_the_lake_and_read_as_one_table(wh) -> None:
    first = datetime(2026, 9, 1, 10, tzinfo=UTC)
    version = {
        "source": "msport",
        "share_code": "ABC",
        "payload_hash": "h1",
        "payload": {"bettableBetSlip": [{"event": {"eventId": "sr:match:1"}}]},
        "followed_times": 5,
        "last_fetched_at": first,
    }
    warehouse.append_slips(wh, [version])
    # The same version fetched again later, with more followers.
    warehouse.append_slips(
        wh, [{**version, "followed_times": 9, "last_fetched_at": first + timedelta(hours=1)}]
    )
    warehouse.connect()  # views are created at open; re-open to see the files
    warehouse._create_lake_views(wh.raw)

    rows = wh.query("SELECT followed_times, first_fetched_at FROM core.bronze_slip_payload")
    assert len(rows) == 1
    assert rows.FOLLOWED_TIMES[0] == 9
    assert rows.FIRST_FETCHED_AT[0] == first
    latest = wh.query("SELECT share_code FROM core.bronze_slip_payload_latest")
    assert list(latest.SHARE_CODE) == ["ABC"]

    assert warehouse.compact_slips(wh) == 2
    warehouse._create_lake_views(wh.raw)
    assert len(wh.query("SELECT * FROM core.bronze_slip_payload")) == 1


def test_percent_placeholders_and_rowcount_match_the_old_connector(wh) -> None:
    now = datetime.now(UTC)
    warehouse.merge_bulk(
        wh,
        table="pipeline_cursor",
        rows=[{"scope": "a", "key": str(i), "position": now} for i in range(3)],
        key=["scope", "key"],
    )
    with wh.cursor() as cur:
        cur.execute("DELETE FROM core.pipeline_cursor WHERE scope = %s AND key <> %s", ("a", "0"))
        assert cur.rowcount == 2
        cur.execute("SELECT key FROM core.pipeline_cursor")
        assert [d[0] for d in cur.description] == ["KEY"]
