from uuid import uuid4

from arbibet_capstone.bronze import latest_payloads


class _FakeCursor:
    def __init__(self, rows: list[tuple[str, bytes]]) -> None:
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
    def __init__(self, rows: list[tuple[str, bytes]]) -> None:
        self.cur = _FakeCursor(rows)

    def cursor(self) -> _FakeCursor:
        return self.cur


def test_bookmaker_names_are_normalised_on_the_way_out() -> None:
    conn = _FakeConnection([("msport", b"{}"), ("bet9ja", b"{}")])

    result = latest_payloads(conn, uuid4())  # type: ignore[arg-type]

    assert set(result) == {"msports", "bet9ja"}


def test_payload_bytes_are_returned_verbatim() -> None:
    body = b'{"D": {"O": {"S_1X2_1": 2.05}}}'
    conn = _FakeConnection([("bet9ja", body)])

    assert latest_payloads(conn, uuid4())["bet9ja"] == body  # type: ignore[arg-type]


def test_the_event_id_is_the_only_bound_parameter() -> None:
    # Guards the access path: a second parameter would mean the query had
    # grown a filter, and the one that matters here is `bookmaker` — no index
    # leads with it, so filtering on it scans the payload bodies.
    conn = _FakeConnection([])
    event_id = uuid4()

    latest_payloads(conn, event_id)  # type: ignore[arg-type]

    assert conn.cur.params == (event_id,)
