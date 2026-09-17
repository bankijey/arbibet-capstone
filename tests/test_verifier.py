"""The verification gate against a real (temporary) DuckDB: verdicts persist, wrong
books are excluded, their signals purged, and the model is asked once."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from runner.verifier import FixtureVerifier

from arbibet_capstone import warehouse
from arbibet_capstone.fixtures import Fixture
from arbibet_capstone.warehouse import merge_bulk

EVENT = UUID("3858072f-22d9-492f-a57d-6600ecf25b2d")
KICKOFF = datetime(2026, 9, 20, 17, 30, tzinfo=UTC)


@pytest.fixture
def wh(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKDB_PATH", str(tmp_path / "test.duckdb"))
    monkeypatch.setenv("LAKE_PATH", str(tmp_path / "lake"))
    monkeypatch.setattr(warehouse, "_database", None)
    yield warehouse.connect()
    warehouse._database.close()
    monkeypatch.setattr(warehouse, "_database", None)


def _payload(text: str) -> SimpleNamespace:
    return SimpleNamespace(payload=text.encode(), fire_time=KICKOFF - timedelta(days=1))


LEVSKI = Fixture(EVENT, KICKOFF, "Levski Sofia", "Ludogorets", "First League", 1, 2, 3, "sr:1")
PAYLOADS = {
    "livescorebet": _payload(json.dumps({"event": {"name": "Levski Sofia - Ludogorets Razgrad"}})),
    "msport": _payload(
        json.dumps(
            {"data": {"homeTeam": "SFC Etar Veliko Tarnovo", "awayTeam": "PFC Chernomorets"}}
        )
    ),
    "betking": _payload(json.dumps({"no": "names"})),  # no parser: never checked
}


def _signals(wh) -> None:
    legs = [
        {"outcome_id": "12", "bookmaker": "livescorebet", "odds": 2.1},
        {"outcome_id": "13", "bookmaker": "msport", "odds": 1.98},
    ]
    merge_bulk(
        wh,
        table="fact_arbitrage_signal",
        rows=[
            {
                "signal_key": "k-bad",
                "event_id": str(EVENT),
                "market_id": "18;2.5",
                "market_base_id": 18,
                "specifier": "2.5",
                "arbitrage": 1.019,
                "n_legs": 2,
                "legs": legs,
                "oldest_leg_fire_time": KICKOFF,
                "newest_leg_fire_time": KICKOFF,
                "leg_spread_seconds": 0,
                "detected_at": KICKOFF,
                "consumed_at": KICKOFF,
            },
            {
                "signal_key": "k-fine",
                "event_id": str(EVENT),
                "market_id": "1",
                "market_base_id": 1,
                "specifier": None,
                "arbitrage": 1.01,
                "n_legs": 2,
                "legs": [
                    {"outcome_id": "1", "bookmaker": "livescorebet", "odds": 2.0},
                    {"outcome_id": "2", "bookmaker": "bet9ja", "odds": 2.1},
                ],
                "oldest_leg_fire_time": KICKOFF,
                "newest_leg_fire_time": KICKOFF,
                "leg_spread_seconds": 0,
                "detected_at": KICKOFF,
                "consumed_at": KICKOFF,
            },
        ],
        key=["signal_key"],
        json_columns={"legs"},
    )
    books = dict(
        wh.raw.execute("SELECT bookmaker_name, bookmaker_id FROM dim_bookmaker").fetchall()
    )
    merge_bulk(
        wh,
        table="fact_ev_signal",
        rows=[
            {
                "signal_key": f"ev-{book}",
                "event_id": str(EVENT),
                "market_id": "1",
                "market_base_id": 1,
                "specifier": None,
                "outcome_id": "1",
                "outcome_name": "home",
                "bookmaker_id": books[book],
                "odds": 2.2,
                "implied_p": 0.5,
                "p_source": "sportybet",
                "ev": 0.1,
                "payload_fire_time": KICKOFF,
                "probability_fire_time": KICKOFF,
                "probability_spread_seconds": 0,
                "detected_at": KICKOFF,
                "consumed_at": KICKOFF,
            }
            for book in ("msport", "livescorebet")
        ],
        key=["signal_key"],
    )


def test_a_wrong_book_is_excluded_recorded_purged_and_reported(wh):
    _signals(wh)
    told: list[tuple[str, str]] = []
    verifier = FixtureVerifier(wh, on_mismatch=lambda f, c: told.append((c.bookmaker, c.verdict)))
    verifier.client = None

    accepted, result = verifier.check(LEVSKI, PAYLOADS)
    assert set(accepted) == {"livescorebet", "betking"}
    assert result.checks["msport"].verdict == "mismatch"
    assert "betking" not in result.checks
    assert told == [("msport", "mismatch")]

    # Recorded with its reason, and the wrong book's signals are gone -- only its.
    rows = wh.query("SELECT bookmaker_name, verdict, method, explanation FROM fixture_check")
    assert dict(zip(rows.BOOKMAKER_NAME, rows.VERDICT, strict=True)) == {
        "livescorebet": "ok",
        "msport": "mismatch",
    }
    assert "Etar" in rows[rows.BOOKMAKER_NAME == "msport"].EXPLANATION.iloc[0]
    keys = set(wh.query("SELECT signal_key FROM fact_arbitrage_signal").SIGNAL_KEY)
    assert keys == {"k-fine"}
    ev = set(wh.query("SELECT signal_key FROM fact_ev_signal").SIGNAL_KEY)
    assert ev == {"ev-livescorebet"}
    assert verifier.mismatched() == {str(EVENT): {"msport"}}

    # Second time: same verdicts, nothing re-reported, and a fresh instance reads them back.
    verifier.check(LEVSKI, PAYLOADS)
    assert told == [("msport", "mismatch")]
    again = FixtureVerifier(wh)
    again.client = None
    assert again.mismatched() == {str(EVENT): {"msport"}}


class _Client:
    def __init__(self, same: bool):
        self.same, self.calls = same, 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kw):
        self.calls += 1
        body = {"books": {"msport": {"same_match": self.same, "reason": "reserve side"}}}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))]
        )


def test_the_model_settles_a_partial_match_once(wh):
    fixture = Fixture(EVENT, KICKOFF, "Flora II", "Kalev", None, None, None, None, None)
    payloads = {
        "msport": _payload(
            json.dumps({"data": {"homeTeam": "Tallinna FC Flora", "awayTeam": "JK Tallinna Kalev"}})
        )
    }
    verifier = FixtureVerifier(wh)
    verifier.client = _Client(same=False)
    accepted, result = verifier.check(fixture, payloads)
    assert accepted == {} and result.checks["msport"].method == "model"
    assert verifier.client.calls == 1
    verifier.check(fixture, payloads)
    assert verifier.client.calls == 1  # remembered, not re-asked
    assert verifier.stats["model_calls"] == 1
