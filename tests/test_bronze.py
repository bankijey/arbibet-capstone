from datetime import UTC, datetime
from uuid import uuid4

from arbibet_capstone.bronze import latest_payloads

_T0 = datetime(2026, 9, 2, 18, 30, tzinfo=UTC)


class _FakeCursor:
    def __init__(self, rows: list[tuple[str, bytes, datetime]]) -> None:
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
    def __init__(self, rows: list[tuple[str, bytes, datetime]]) -> None:
        self.cur = _FakeCursor(rows)

    def cursor(self) -> _FakeCursor:
        return self.cur


def test_bronze_bookmaker_names_are_returned_unchanged() -> None:
    # Bronze is downstream of arbibet-markets' alias map, so its spelling is
    # already canonical. The reader must not invent a second vocabulary.
    conn = _FakeConnection([("msport", b"{}", _T0), ("ilotbet", b"{}", _T0)])

    result = latest_payloads(conn, uuid4())  # type: ignore[arg-type]

    assert set(result) == {"msport", "ilotbet"}


def test_payload_bytes_are_returned_verbatim() -> None:
    body = b'{"D": {"O": {"S_1X2_1": 2.05}}}'
    conn = _FakeConnection([("bet9ja", body, _T0)])

    assert latest_payloads(conn, uuid4())["bet9ja"].payload == body  # type: ignore[arg-type]


def test_fire_time_travels_with_each_payload() -> None:
    # Without this the arbitrage consumer cannot compute leg_spread_seconds,
    # and a stale leg becomes indistinguishable from a fresh one.
    older = datetime(2026, 9, 2, 18, 10, tzinfo=UTC)
    conn = _FakeConnection([("sportybet", b"{}", _T0), ("bet9ja", b"{}", older)])

    result = latest_payloads(conn, uuid4())  # type: ignore[arg-type]

    assert result["sportybet"].fire_time == _T0
    assert result["bet9ja"].fire_time == older


def test_the_event_id_is_the_only_bound_parameter() -> None:
    # Guards the access path: a second parameter would mean the query had
    # grown a filter, and the one that matters here is `bookmaker` — no index
    # leads with it, so filtering on it scans the payload bodies.
    conn = _FakeConnection([])
    event_id = uuid4()

    latest_payloads(conn, event_id)  # type: ignore[arg-type]

    assert conn.cur.params == (event_id,)
