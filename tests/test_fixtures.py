from datetime import UTC, datetime
from uuid import uuid4

from arbibet_capstone.fixtures import Fixture, upcoming

_SINCE = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_UNTIL = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
_KICKOFF = datetime(2026, 9, 2, 19, 0, tzinfo=UTC)


class _FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows
        self.params: tuple[object, ...] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.params = params

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _FakeConnection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.cur = _FakeCursor(rows)

    def cursor(self) -> _FakeCursor:
        return self.cur


def test_a_row_maps_onto_both_identity_spaces() -> None:
    event_id = uuid4()
    conn = _FakeConnection(
        [(event_id, _KICKOFF, "Man City", "Porto", "UCL", 1499622, 50, 212, "sr:match:1")]
    )

    (fixture,) = upcoming(conn, since=_SINCE, until=_UNTIL)  # type: ignore[arg-type]

    assert fixture == Fixture(
        event_id=event_id,
        kickoff=_KICKOFF,
        home_team="Man City",
        away_team="Porto",
        tournament="UCL",
        apifootball_id=1499622,
        home_team_id=50,
        away_team_id=212,
        sr_match_id="sr:match:1",
    )


def test_a_fixture_without_an_apifootball_leg_survives_with_nulls() -> None:
    # The matcher resolves an apifootball leg for most fixtures, not all. Such
    # a fixture is still priceable -- it simply has no history behind it -- so
    # it must not be silently dropped.
    conn = _FakeConnection(
        [(uuid4(), _KICKOFF, "Bet9ja FC", "Some Team", "NPFL", None, None, None, None)]
    )

    (fixture,) = upcoming(conn, since=_SINCE, until=_UNTIL)  # type: ignore[arg-type]

    assert fixture.apifootball_id is None
    assert fixture.home_team_id is None
    assert fixture.home_team == "Bet9ja FC"


def test_the_window_and_sport_are_the_only_bound_parameters() -> None:
    # Guards the access path: these three are exactly what
    # event_matches_sport_start_idx (sport_key, start) serves.
    conn = _FakeConnection([])

    upcoming(conn, since=_SINCE, until=_UNTIL)  # type: ignore[arg-type]

    assert conn.cur.params == ("football", _SINCE, _UNTIL)
