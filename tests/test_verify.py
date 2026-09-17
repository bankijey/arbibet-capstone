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
    def __init__(self, answer: dict, fail: bool = False, second: dict | None = None):
        self.answer, self.fail, self.calls = answer, fail, 0
        self.second = second  # what the escalation model answers, if asked
        self.models: list[str] = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kw):
        self.calls += 1
        self.models.append(kw.get("model"))
        if self.fail:
            raise RuntimeError("down")
        answer = self.second if (self.second is not None and self.calls > 1) else self.answer
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))]
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
    result = verify.verify(fixture, payloads, client, escalate=None)
    assert client.calls == 1
    assert result.checks["ilotbet"].verdict == "mismatch"
    assert result.checks["ilotbet"].method == "model"
    assert "Atletico" in result.checks["ilotbet"].explanation
    # livescorebet mismatched by names alone; the model was not asked about it.
    assert result.checks["livescorebet"].verdict == "mismatch"
    # Known verdicts are reused while the book names the same teams.
    again = verify.verify(fixture, payloads, client, known=result.checks, escalate=None)
    assert client.calls == 1 and again.checks["ilotbet"].verdict == "mismatch"
    # The model failing leaves the book unverified, not ok.
    down = verify.verify(fixture, payloads, _Client({}, fail=True))
    assert down.checks["ilotbet"].verdict == "unverified"
    assert verify.verify(fixture, payloads, None).checks["ilotbet"].verdict == "unverified"


def test_a_mismatch_from_the_small_model_needs_the_larger_models_confirmation():
    payloads = {"msport": json.dumps({"data": {"homeTeam": "We SC", "awayTeam": "El Seka"}})}
    fixture = {"home": "Itesalat", "away": "El Seka El Hadid"}
    small_no = {"books": {"msport": {"same_match": False, "reason": "different club"}}}
    large_yes = {
        "books": {"msport": {"same_match": True, "reason": "We SC is Itesalat's new name"}}
    }
    client = _Client(small_no, second=large_yes)
    result = verify.verify(fixture, payloads, client, model="small", escalate="large")
    assert client.models == ["small", "large"]
    assert result.checks["msport"].verdict == "ok"
    assert "new name" in result.checks["msport"].explanation
    # Both say different: excluded, with the larger model's reason.
    large_no = {"books": {"msport": {"same_match": False, "reason": "another club entirely"}}}
    result = verify.verify(fixture, payloads, _Client(small_no, second=large_no), escalate="large")
    assert result.checks["msport"].verdict == "mismatch"
    assert "another club" in result.checks["msport"].explanation
    # A "same match" from the small model is not escalated.
    yes = _Client({"books": {"msport": {"same_match": True, "reason": "alias"}}}, second=large_no)
    assert verify.verify(fixture, payloads, yes, escalate="large").checks["msport"].verdict == "ok"
    assert yes.calls == 1


def test_row_shape():
    check = Check("msport", "A", "B", "mismatch", "model", "different match")
    r = verify.row("e", check, None, "gpt-4o-mini")
    assert r["bookmaker_name"] == "msport" and r["model"].startswith("gpt-4o-mini#")
    assert verify.row("e", Check("m", "A", "B", "ok", "names", "x"), None, "m")["model"] is None


def test_books_that_agree_with_each_other_outvote_a_stale_label():
    # Cuniburo renamed itself Vinotinto FC; API-Football still says Cuniburo.
    payloads = {
        b: json.dumps({"data": {"homeTeam": "Vinotinto FC Ecuador", "awayTeam": "9 de Octubre"}})
        for b in ("bet9ja", "msport", "sportybet")
    }
    result = verify.verify({"home": "Cuniburo", "away": "9 de Octubre"}, payloads, None)
    assert {c.verdict for c in result.checks.values()} == {"ok"}
    assert result.checks["msport"].method == "consensus"
    # A lone book against the label and the other books stays out (Levski).
    result = verify.verify(FIXTURE, PAYLOADS, None)
    assert result.checks["livescorebet"].verdict == "ok"
    assert set(result.mismatched) == {"sportybet", "msport", "bet9ja"}
    # Consensus never applies while any book matches the label.
    mixed = dict(PAYLOADS)
    mixed["ilotbet"] = json.dumps({"data": {"homeTeam": "Real Madrid", "awayTeam": "Getafe"}})
    result = verify.verify(FIXTURE, mixed, None)
    assert result.checks["ilotbet"].verdict == "mismatch"
    assert set(result.mismatched) == {"sportybet", "msport", "bet9ja", "ilotbet"}


def test_model_rows_carry_the_prompt_version():
    check = Check("msport", "A", "B", "mismatch", "model", "different")
    assert verify.row("e", check, None, "gpt-4o-mini")["model"] == (
        f"gpt-4o-mini#{verify.PROMPT_VERSION}"
    )


def test_livescorebet_match_name_beats_its_competition_name():
    payload = json.dumps(
        {
            "header": {"category": {"name": "Austria - Bundesliga"}},
            "event": {"categoryName": "Austria - Bundesliga", "name": "Rapid Wien - WSG Tirol"},
        }
    )
    assert team_names("livescorebet", payload) == ("Rapid Wien", "WSG Tirol")
