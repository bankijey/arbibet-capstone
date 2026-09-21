"""Pass 2 of settlement: API-Football confirms, corrects, disputes and extends pass 1."""

from __future__ import annotations

from datetime import UTC, datetime

from runner.telegram.settle import correct_settled_bets

from arbibet_capstone.confirm_settle import (
    COLUMNS,
    Verdict,
    match_record,
    names_agree,
    reconcile,
    settle_outcome,
)

NOW = datetime(2026, 9, 21, 4, 10, tzinfo=UTC)
KEY = {
    "event_id": "e1",
    "market_family": "1x2",
    "period": "match",
    "side_or_line": "home",
    "time_basis": "regular",
}


def _match(status="FT", reg=(2, 1), h1=(1, 0), full=None, corners=(6, 5), **extra):
    full = full or reg
    return {
        "status": status,
        "team_name": "Kallithea",
        "opponent_name": "Panserraikos",
        "goals_for_h1": h1[0],
        "goals_against_h1": h1[1],
        "goals_for_h2": reg[0] - h1[0],
        "goals_against_h2": reg[1] - h1[1],
        "goals_for_reg": reg[0],
        "goals_against_reg": reg[1],
        "goals_for_et": full[0] - reg[0] if status != "FT" else None,
        "goals_against_et": full[1] - reg[1] if status != "FT" else None,
        "goals_for_pens": None,
        "goals_against_pens": None,
        "goals_for_full": full[0],
        "goals_against_full": full[1],
        "corners": corners[0] if corners else None,
        "corners_against": corners[1] if corners else float("nan"),
        **extra,
    }


def _provisional(verdict="won"):
    return {
        "verdict": verdict,
        "reason": None,
        "stage": "provisional",
        "source": "msport",
        "score": "2:1 (HT 1:0)",
        "settled_at": datetime(2026, 9, 20, 19, 5, tzinfo=UTC),
        "previous_verdict": None,
    }


def test_only_a_finished_match_is_a_record_and_nulls_stay_null():
    assert match_record(_match(status="NS")) is None and match_record(_match(status="1H")) is None
    record = match_record(_match(corners=None))
    assert record.status == "FT" and record.corners is None  # NaN is a NULL, not a number
    assert record.scores.reg.home == 2 and record.scores.et is None
    assert record.score_text() == "2:1 (HT 1:0)"


def test_extra_time_settles_on_the_regulation_score():
    # 1:1 after 90 minutes, 2:1 after extra time: the 1x2 market is a draw.
    record = match_record(_match(status="AET", reg=(1, 1), h1=(0, 1), full=(2, 1)))
    assert settle_outcome(record, "1x2", "match", "regular", "draw").verdict == "won"
    assert settle_outcome(record, "1x2", "match", "regular", "home").verdict == "lost"
    assert settle_outcome(record, "total_goals", "match", "regular", "over@2.5").verdict == "lost"
    assert record.score_text() == "1:1 (HT 0:1) AET 2:1"


def test_corners_settle_from_statistics_and_refuse_without_them():
    record = match_record(_match(corners=(6, 5)))
    assert settle_outcome(record, "total_corners", "match", "regular", "over@9.5").verdict == "won"
    assert (
        settle_outcome(record, "total_corners", "match", "regular", "under@9.5").verdict == "lost"
    )
    assert settle_outcome(record, "total_corners", "match", "regular", "over@11").verdict == "push"
    refused = [
        settle_outcome(
            match_record(_match(corners=None)), "total_corners", "match", "regular", "over@9.5"
        ),
        settle_outcome(record, "total_corners", "1h", "1h", "over@4.5"),
        # The count includes extra time, the market does not.
        settle_outcome(
            match_record(_match(status="AET", reg=(1, 1), full=(2, 1))),
            "total_corners",
            "match",
            "regular",
            "over@9.5",
        ),
        settle_outcome(record, "total_corners", "match", "regular", "odd"),
    ]
    assert [v.verdict for v in refused] == ["unsettleable"] * 4
    assert [v.reason for v in refused] == [
        "no_corner_statistics",
        "corners_by_period_unavailable",
        "corners_include_extra_time",
        "line_invalid",
    ]


def test_agreement_confirms_and_keeps_pass_ones_record():
    record = match_record(_match())
    row = reconcile(KEY, _provisional("won"), Verdict("won"), record, True, NOW)
    assert tuple(row) == COLUMNS
    assert row["stage"] == "confirmed" and row["verdict"] == "won"
    assert row["source"] == "msport" and row["settled_at"].day == 20
    assert row["confirmed_by"] == "apifootball" and row["confirmed_at"] == NOW
    assert row["confirmed_score"] == "2:1 (HT 1:0)" and row["previous_verdict"] is None


def test_disagreement_corrects_only_when_the_teams_are_the_fixtures():
    record = match_record(_match(reg=(1, 1), h1=(1, 0)))
    corrected = reconcile(KEY, _provisional("won"), Verdict("lost"), record, True, NOW)
    assert corrected["stage"] == "corrected" and corrected["verdict"] == "lost"
    assert corrected["previous_verdict"] == "won" and corrected["score"] == "2:1 (HT 1:0)"
    assert corrected["confirmed_score"] == "1:1 (HT 1:0)"
    # API-Football names other teams: as likely a wrong link as a wrong score.
    disputed = reconcile(KEY, _provisional("won"), Verdict("lost"), record, False, NOW)
    assert disputed["stage"] == "disputed" and disputed["verdict"] == "won"
    assert disputed["confirmed_at"] is None and disputed["confirmed_score"] == "1:1 (HT 1:0)"
    assert tuple(disputed) == COLUMNS
    assert names_agree(record, "Kallithea FC", "Panserraikos") is True
    assert names_agree(record, "Levski Sofia", "Panserraikos") is False


def test_what_pass_one_never_settled_is_written_as_confirmed():
    record = match_record(_match())
    row = reconcile(KEY, None, Verdict("won"), record, True, NOW)
    assert tuple(row) == COLUMNS
    assert row["stage"] == "confirmed" and row["source"] == "apifootball"
    assert row["settled_at"] == NOW and row["score"] == "2:1 (HT 1:0)"


class _Store:
    def __init__(self, bets):
        self._bets, self.moves, self.corrected = bets, [], []

    def settled_bets(self, since):
        return self._bets

    def correct_bet(self, bet_id, legs, profit, note):
        self.corrected.append((bet_id, legs, profit, note))

    def move_balances(self, chat_id, mode, deltas):
        self.moves.append((chat_id, mode, dict(deltas)))


class _Warehouse:
    def __init__(self, verdicts):
        self._verdicts = verdicts

    def query(self, sql, params):
        import pandas as pd

        return pd.DataFrame(
            {"OUTCOME_ID": list(self._verdicts), "VERDICT": list(self._verdicts.values())}
        )


def _bet(verdict_1, payout_1, verdict_2, payout_2):
    return {
        "bet_id": 8,
        "chat_id": 1,
        "mode": "real",
        "event_id": "e1",
        "market_id": "1",
        "legs": [
            {
                "book": "msport",
                "outcomeId": "1",
                "outcome": "home",
                "odds": 2.0,
                "stake": 5000.0,
                "verdict": verdict_1,
                "payout": payout_1,
            },
            {
                "book": "livescorebet",
                "outcomeId": "3",
                "outcome": "away",
                "odds": 2.1,
                "stake": 4700.0,
                "verdict": verdict_2,
                "payout": payout_2,
            },
        ],
    }


def test_a_corrected_verdict_re_pays_the_bet_once():
    # Paid as home won; API-Football says away won.
    store = _Store([_bet("won", 10000.0, "lost", 0.0)])
    warehouse = _Warehouse({"1": "lost", "3": "won"})
    assert correct_settled_bets(store, warehouse, NOW) == 1
    assert store.moves == [(1, "real", {"msport": -10000.0, "livescorebet": 9870.0})]
    bet_id, legs, profit, note = store.corrected[0]
    assert bet_id == 8 and round(profit, 2) == 170.0
    assert [leg["verdict"] for leg in legs] == ["lost", "won"]
    assert "home at msport won -> lost" in note and "(balance -130.00)" in note
    # The stored bet now agrees with the warehouse: nothing more to do.
    settled = _Store([_bet("lost", 0.0, "won", 9870.0)])
    assert correct_settled_bets(settled, warehouse, NOW) == 0 and not settled.moves
    # A verdict that went missing is not a correction.
    assert (
        correct_settled_bets(_Store([_bet("won", 10000.0, "lost", 0.0)]), _Warehouse({}), NOW) == 0
    )
