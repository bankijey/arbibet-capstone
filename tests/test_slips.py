import httpx
import pytest

from arbibet_capstone.slips import Settings, list_codes, payload_hash

_FAST = Settings(max_pages=10, delay_seconds=0.0, timeout_seconds=5.0)


def _page(codes: list[str], has_more: int) -> dict[str, object]:
    return {
        "data": {
            "hasMore": has_more,
            "codeList": [
                {"shareCode": c, "id": f"id-{c}", "followedTimes": 100, "folds": 3}
                for c in codes
            ],
        }
    }


def _client(pages: list[dict[str, object]]) -> tuple[httpx.Client, list[object]]:
    """A client that serves `pages` in order, recording each request body."""
    bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json=pages[min(len(bodies) - 1, len(pages) - 1)])

    return httpx.Client(transport=httpx.MockTransport(handler)), bodies


def test_paging_uses_the_lastid_cursor() -> None:
    # Every other paging shape -- pageNum, page, pageSize -- is silently
    # ignored by msport and returns page one, so the cursor is the contract.
    client, bodies = _client([_page(["A", "B"], 1), _page(["C", "D"], 0)])
    with client:
        found = list_codes(client, _FAST)

    assert [s.share_code for s in found] == ["A", "B", "C", "D"]
    assert bodies[0] == {}
    assert bodies[1] == {"lastId": "id-B"}


def test_a_page_that_repeats_its_predecessor_stops_the_fetch() -> None:
    # If paging silently breaks, the endpoint keeps returning page one. Without
    # this guard the fetch would fetch the same twenty codes ten times and look
    # like a successful deep fetch.
    client, _ = _client([_page(["A", "B"], 1)])
    with client:
        found = list_codes(client, _FAST)

    assert [s.share_code for s in found] == ["A", "B"]


def test_has_more_zero_ends_the_fetch() -> None:
    client, bodies = _client([_page(["A"], 0)])
    with client:
        found = list_codes(client, _FAST)

    assert len(found) == 1
    assert len(bodies) == 1


def test_max_pages_caps_the_fetch() -> None:
    # The budget is a promise to a third party, not a hint.
    pages = [_page([f"C{i}"], 1) for i in range(50)]
    client, bodies = _client(pages)
    with client:
        list_codes(client, Settings(max_pages=3, delay_seconds=0.0, timeout_seconds=5))

    assert len(bodies) == 3


def test_followed_times_is_inside_the_hash() -> None:
    # A slip gaining followers is a change worth a row: popularity over time is
    # one of the questions this data exists to answer.
    a = {"shareCode": "X", "followedTimes": 100}
    b = {"shareCode": "X", "followedTimes": 101}

    assert payload_hash(a) != payload_hash(b)


def test_the_hash_ignores_key_order() -> None:
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})


@pytest.mark.parametrize("status", [500, 404])
def test_a_failed_list_page_raises(status: int) -> None:
    # The list is the fetch's spine; a failure here means no slips at all, and
    # silently returning an empty fetch would read as "msport had nothing".
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json={}))
    )
    with client, pytest.raises(httpx.HTTPStatusError):
        list_codes(client, _FAST)
