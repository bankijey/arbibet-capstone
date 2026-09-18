"""The event page's pure parts: one fixture's slice of the signals document, and its links."""

from __future__ import annotations

from dashboard.common import event_link, fair_link
from dashboard.event_signals import for_event, header
from runner.telegram.bot import event_url

DOC = {
    "arbitrage": {
        "legs": [
            {
                "eventId": "e1",
                "marketId": "18;2.5",
                "fixture": "A v B",
                "kickoffAt": "2026-09-20T17:30:00Z",
            },
            {"eventId": "e2", "marketId": "1", "fixture": "C v D", "kickoffAt": None},
        ],
        "tracks": {"e1|18;2.5": {"total": 3, "points": []}, "e2|1": {"total": 1, "points": []}},
    },
    "ev": {
        "rows": [{"eventId": "e1", "marketId": "1", "outcomeId": "1", "fixture": "A v B"}],
        "prices": {"e1|1|1": {"msport": [["2026-09-18T10:00:00Z", 2.1]]}, "e2|1|2": {}},
    },
}


def test_one_fixtures_slice_of_the_signals_document():
    parts = for_event(DOC, "e1")
    assert [leg["marketId"] for leg in parts["legs"]] == ["18;2.5"]
    assert list(parts["tracks"]) == ["18;2.5"]  # keyed by market within the event
    assert len(parts["ev"]) == 1 and list(parts["prices"]) == ["1|1"]
    assert header(parts) == ("A v B", "2026-09-20T17:30:00Z")
    empty = for_event(DOC, "nope")
    assert empty == {"legs": [], "tracks": {}, "ev": [], "prices": {}} and header(empty) is None
    assert for_event({}, "e1")["legs"] == []


def test_links():
    assert (
        fair_link("https://www.sportybet.com/x", 0.47)
        == "https://www.sportybet.com/x#fair-odds-2.13"
    )
    assert fair_link(None, 0.47) is None and fair_link("https://x", None) is None
    assert fair_link("https://x", 0) is None
    assert event_link("e1") == "/fixture?event_id=e1" and event_link(None) is None
    assert event_url("https://arbibet.streamlit.app/", "e1") == (
        "https://arbibet.streamlit.app/fixture?event_id=e1"
    )
