"""The review queue against a real (temporary) DuckDB: proposals persist, nothing is
excluded until a decision, a decision purges, and the model is asked once."""

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
    monkeypatch.setenv("LOCAL_DIR", str(tmp_path / "local"))
    monkeypatch.setenv("VERIFY_WITH_MODEL", "0")
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
    merge_bulk(
        wh,
        table="fact_arbitrage_signal",
        rows=[
            {
                "signal_key": key,
                "event_id": str(EVENT),
                "market_id": market,
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
            }
            for key, market, legs in (
                (
                    "k-bad",
                    "18;2.5",
                    [
                        {"outcome_id": "12", "bookmaker": "livescorebet", "odds": 2.1},
                        {"outcome_id": "13", "bookmaker": "msport", "odds": 1.98},
                    ],
                ),
                (
                    "k-fine",
                    "1",
                    [
                        {"outcome_id": "1", "bookmaker": "livescorebet", "odds": 2.0},
                        {"outcome_id": "2", "bookmaker": "bet9ja", "odds": 2.1},
                    ],
                ),
            )
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


def _keys(wh, table: str) -> set[str]:
    return set(wh.query(f"SELECT signal_key FROM {table}").SIGNAL_KEY)


def test_a_wrong_book_is_only_proposed_until_a_person_decides(wh, tmp_path):
    _signals(wh)
    verifier = FixtureVerifier(wh)

    accepted, result = verifier.check(LEVSKI, PAYLOADS)
    # Proposed, not excluded: every payload still goes through, nothing is purged.
    assert set(accepted) == {"livescorebet", "msport", "betking"}
    assert result.checks["msport"].verdict == "candidate"
    assert "betking" not in result.checks
    assert verifier.mismatched() == {}
    assert _keys(wh, "fact_arbitrage_signal") == {"k-bad", "k-fine"}
    rows = wh.query("SELECT bookmaker_name, verdict, explanation FROM fixture_check")
    assert dict(zip(rows.BOOKMAKER_NAME, rows.VERDICT, strict=True)) == {
        "livescorebet": "ok",
        "msport": "candidate",
    }
    # ...and the queue file carries it, with the fixture named.
    queue = json.loads((tmp_path / "local" / "fixture_checks.json").read_text(encoding="utf-8"))
    (row,) = (c for c in queue["checks"] if c["book"] == "msport")
    assert row["verdict"] == "candidate" and row["fixture"] == "Levski Sofia v Ludogorets"

    # The person excludes it: applied, purged, and the check now drops the book.
    (tmp_path / "local" / "decisions.json").write_text(
        json.dumps(
            [{"eventId": str(EVENT), "book": "msport", "verdict": "mismatch", "note": "merged"}]
        ),
        encoding="utf-8",
    )
    assert verifier.apply_decisions() == 1
    assert verifier.mismatched() == {str(EVENT): {"msport"}}
    assert _keys(wh, "fact_arbitrage_signal") == {"k-fine"}
    assert _keys(wh, "fact_ev_signal") == {"ev-livescorebet"}
    accepted, result = verifier.check(LEVSKI, PAYLOADS)
    assert set(accepted) == {"livescorebet", "betking"}
    assert result.checks["msport"].verdict == "mismatch"
    assert result.checks["msport"].method == "review"
    # Unchanged file: nothing re-applied. A fresh instance reads the decision back.
    assert verifier.apply_decisions() == 0
    again = FixtureVerifier(wh)
    assert again.mismatched() == {str(EVENT): {"msport"}}
    note = wh.query("SELECT note, reviewed_at FROM fixture_check WHERE bookmaker_name = 'msport'")
    assert note.NOTE.iloc[0] == "merged" and note.REVIEWED_AT.notna().all()

    # A re-check never overwrites the decision or its note.
    verifier.check(LEVSKI, PAYLOADS)
    note = wh.query("SELECT verdict, note FROM fixture_check WHERE bookmaker_name = 'msport'")
    assert note.VERDICT.iloc[0] == "mismatch" and note.NOTE.iloc[0] == "merged"


def test_clearing_keeps_the_book_and_reversing_works(wh, tmp_path):
    verifier = FixtureVerifier(wh)
    verifier.check(LEVSKI, PAYLOADS)
    decisions = tmp_path / "local" / "decisions.json"
    decisions.write_text(
        json.dumps([{"eventId": str(EVENT), "book": "msport", "verdict": "cleared"}]),
        encoding="utf-8",
    )
    assert verifier.apply_decisions() == 1
    accepted, result = verifier.check(LEVSKI, PAYLOADS)
    assert "msport" in accepted and result.checks["msport"].verdict == "cleared"
    # A newer decision the other way is applied too.
    import os
    import time

    decisions.write_text(
        json.dumps([{"eventId": str(EVENT), "book": "msport", "verdict": "mismatch"}]),
        encoding="utf-8",
    )
    os.utime(decisions, (time.time() + 5, time.time() + 5))
    assert verifier.apply_decisions() == 1
    assert verifier.mismatched() == {str(EVENT): {"msport"}}


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


def test_the_model_proposes_a_partial_match_once(wh):
    fixture = Fixture(EVENT, KICKOFF, "Flora II", "Kalev", None, None, None, None, None)
    payloads = {
        "msport": _payload(
            json.dumps({"data": {"homeTeam": "Tallinna FC Flora", "awayTeam": "JK Tallinna Kalev"}})
        )
    }
    verifier = FixtureVerifier(wh)
    verifier.client = _Client(same=False)
    verifier.escalate = None  # the confirmation step is tested in test_verify
    accepted, result = verifier.check(fixture, payloads)
    assert set(accepted) == {"msport"}  # a proposal excludes nothing
    assert result.checks["msport"].verdict == "candidate"
    assert result.checks["msport"].method == "model"
    assert verifier.client.calls == 1
    verifier.check(fixture, payloads)
    assert verifier.client.calls == 1  # remembered, not re-asked
    assert verifier.stats["model_calls"] == 1


def test_a_fixture_flagged_whole_yields_nothing_until_restored(wh, tmp_path):
    _signals(wh)
    verifier = FixtureVerifier(wh)
    changed: list[int] = []
    verifier.on_change = lambda: changed.append(1)

    removed = verifier.flag_fixture(str(EVENT), True, "chat 7", "Levski Sofia v Ludogorets")
    assert removed == 4  # both arbitrage signals and both EV signals
    assert _keys(wh, "fact_arbitrage_signal") == set() == _keys(wh, "fact_ev_signal")
    accepted, result = verifier.check(LEVSKI, PAYLOADS)
    assert accepted == {} and list(result.checks) == ["*"]
    assert verifier.mismatched() == {str(EVENT): {"*"}}
    assert list(verifier.flagged_fixtures()) == [str(EVENT)] and changed == [1]
    # The review file names it, and a fresh instance still excludes it.
    queue = json.loads((tmp_path / "local" / "fixture_checks.json").read_text(encoding="utf-8"))
    (row,) = (c for c in queue["checks"] if c["book"] == "*")
    assert row["verdict"] == "mismatch" and row["fixture"] == "Levski Sofia v Ludogorets"
    assert FixtureVerifier(wh).check(LEVSKI, PAYLOADS)[0] == {}

    verifier.flag_fixture(str(EVENT), False, "chat 7")
    accepted, _ = verifier.check(LEVSKI, PAYLOADS)
    assert "livescorebet" in accepted and verifier.flagged_fixtures() == {}
    # What each book lists is available to show the person deciding.
    assert verifier.book_names(str(EVENT))["msport"][0] == "SFC Etar Veliko Tarnovo"
