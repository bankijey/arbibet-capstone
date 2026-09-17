"""Match verification: the names check, payload name extraction, and the model's verdicts."""

from __future__ import annotations

import json
from types import SimpleNamespace

from arbibet_capstone import verify
from arbibet_capstone.verify import (
    Check,
    by_names,
    describe,
    normalise,
    similarity,
    team_names,
)

FIXTURE = {
    "home": "Levski Sofia",
    "away": "Ludogorets",
    "tournament": "First League",
    "kickoff": "2026-09-20 17:30 UTC",
}

PAYLOADS = {
    "sportybet": json.dumps(
        {
            "data": {
                "eventId": "sr:match:72466520",
                "homeTeamName": "SFC Etar Veliko Tarnovo",
                "awayTeamName": "PFC Chernomorets Burgas",
            }
        }
    ),
    "msport": json.dumps(
        {"data": {"homeTeam": "SFC Etar Veliko Tarnovo", "awayTeam": "PFC Chernomorets Burgas"}}
    ),
    "bet9ja": json.dumps({"R": "OK", "D": {"DS": "Etar Veliko Tarnovo - Chernomorets Burgas"}}),
    "livescorebet": json.dumps({"event": {"name": "Levski Sofia - Ludogorets Razgrad"}}),
}


def test_team_names_from_every_book_shape():
    assert team_names("sportybet", PAYLOADS["sportybet"]) == (
        "SFC Etar Veliko Tarnovo",
        "PFC Chernomorets Burgas",
    )
    assert team_names("msport", PAYLOADS["msport"])[0] == "SFC Etar Veliko Tarnovo"
    assert team_names("bet9ja", PAYLOADS["bet9ja"]) == (
        "Etar Veliko Tarnovo",
        "Chernomorets Burgas",
    )
    assert team_names("livescorebet", PAYLOADS["livescorebet"]) == (
        "Levski Sofia",
        "Ludogorets Razgrad",
    )
    assert team_names("x", b"not json") is None
    assert team_names("x", json.dumps({"markets": []})) is None


def test_similarity_ignores_prefixes_years_and_transliteration():
    assert normalise("PFC Ludogorets 1945 Razgrad") == {"ludogorets", "razgrad"}
    assert similarity("Ludogorets", "PFC Ludogorets 1945 Razgrad") == 1.0
    assert similarity("Levski Sofia", "PFC Levski Sofia") == 1.0
    assert similarity("Chernomorets Burgas", "FC Chernomorec 1919 Burgas") == 1.0
    assert similarity("Levski Sofia", "SFC Etar Veliko Tarnovo") == 0.0
    # One shared token of two is ambiguous, never a clear match: the model decides.
    assert similarity("Manchester United", "Manchester City") == 0.5
    assert similarity("Real Madrid", "Atletico Madrid") == 0.5


def test_names_check_finds_the_merged_event():
    names = {book: team_names(book, raw) for book, raw in PAYLOADS.items()}
    result = by_names(FIXTURE["home"], FIXTURE["away"], names)
    assert result.ok == {"livescorebet"}
    assert set(result.mismatched) == {"sportybet", "msport", "bet9ja"}
    assert "msport lists SFC Etar Veliko Tarnovo v PFC Chernomorets Burgas" in describe(
        "Levski v Ludogorets", result.checks.values()
    )
    # Partial agreement is left for the model.
    partial = by_names("Real Madrid", "Barcelona", {"b": ("Atletico Madrid", "Barcelona")})
    assert partial.checks["b"].verdict == "unverified"
    # No names at all is unverified, never a mismatch.
    assert by_names("A", "B", {"b": None}).checks["b"].verdict == "unverified"


class _Client:
    def __init__(self, answer: dict, fail: bool = False):
        self.answer, self.fail, self.calls = answer, fail, 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.answer)))]
        )


def test_model_is_asked_once_for_the_ambiguous_books_only():
    payloads = {
        "livescorebet": PAYLOADS["livescorebet"],
        "ilotbet": json.dumps({"data": {"homeTeam": "Atl. Madrid", "awayTeam": "Barcelona"}}),
    }
    fixture = {"home": "Real Madrid", "away": "Barcelona"}
    client = _Client(
        {"books": {"ilotbet": {"same_match": False, "reason": "Atletico is another club"}}}
    )
    result = verify.verify(fixture, payloads, client)
    assert client.calls == 1
    assert result.checks["ilotbet"].verdict == "mismatch"
    assert result.checks["ilotbet"].method == "model"
    assert "Atletico" in result.checks["ilotbet"].explanation
    # livescorebet mismatched by names alone; the model was not asked about it.
    assert result.checks["livescorebet"].verdict == "mismatch"
    # Known verdicts are reused while the book names the same teams.
    again = verify.verify(fixture, payloads, client, known=result.checks)
    assert client.calls == 1 and again.checks["ilotbet"].verdict == "mismatch"
    # The model failing leaves the book unverified, not ok.
    down = verify.verify(fixture, payloads, _Client({}, fail=True))
    assert down.checks["ilotbet"].verdict == "unverified"
    assert verify.verify(fixture, payloads, None).checks["ilotbet"].verdict == "unverified"


def test_row_shape():
    check = Check("msport", "A", "B", "mismatch", "model", "different match")
    r = verify.row("e", check, None, "gpt-4o-mini")
    assert r["bookmaker_name"] == "msport" and r["model"] == "gpt-4o-mini"
    assert verify.row("e", Check("m", "A", "B", "ok", "names", "x"), None, "m")["model"] is None
