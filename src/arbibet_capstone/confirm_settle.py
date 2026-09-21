"""Pass 2 of settlement: API-Football confirms, corrects and extends pass 1.

Pass 1 (`fast_settle`) settles from msport's final score minutes after full
time and marks every verdict PROVISIONAL. This pass runs once API-Football's
results are in the warehouse (the ingestor's daily run at 02:00 UTC, flattened
by the cold loop at 04:00) and re-settles the same demand -- every outcome an
EV signal, a surebet leg, a slip leg or a wallet bet refers to -- from
`fact_team_match`. Per outcome it does one of four things:

    confirmed   pass 1 said the same. The verdict stands; the row records who
                confirmed it, when, and on what score.
    corrected   pass 1 said otherwise. API-Football wins, the old verdict is
                kept in `previous_verdict`, and wallet bets paid on it are
                re-paid (runner/telegram/settle.py).
    disputed    pass 1 said otherwise, but the teams API-Football names are not
                recognisably the fixture's. A wrong link is as likely as a
                wrong score, so nothing changes; it is counted and retried.
    (new row)   pass 1 never settled it: no msport id, unverified names, extra
                time, a fixture older than pass 1's window -- or a market a
                scoreline cannot decide. Written straight in as `confirmed`.

WHAT IT EXTENDS. API-Football publishes what msport's two scores cannot give:
extra time and penalties as their own periods, so a cup tie settles on the
right score; and, where it publishes match statistics, corners -- so
`total_corners` settles here, outside the vendored engine, and only for a
match that ended in normal time (the count includes any extra time played).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from arbibet_capstone.crosswalk.settle.settlement import (
    PeriodResolvedScores,
    TeamScore,
    parse_side_or_line,
    settle,
)
from arbibet_capstone.verify import CLEAR_MATCH, similarity

SOURCE = "apifootball"

# API-Football's finished statuses, in the engine's vocabulary (spark/settle.py).
STATUS_TO_ENGINE = {"FT": "ft", "AET": "aet", "PEN": "pen"}

CORNER_FAMILIES = {"total_corners"}


@dataclass(frozen=True)
class Verdict:
    verdict: str  # won | lost | push | void | half_win | half_loss | unsettleable
    reason: str | None = None


@dataclass(frozen=True)
class MatchRecord:
    """One finished fixture as API-Football has it, from the fixture's home team's row."""

    status: str  # FT | AET | PEN
    scores: PeriodResolvedScores
    corners: int | None  # both teams'; None when no statistics were published
    home: str | None
    away: str | None

    def score_text(self) -> str | None:
        reg, h1 = self.scores.reg, self.scores.h1
        if reg is None:
            return None
        text = f"{reg.home}:{reg.away}"
        if h1 is not None:
            text += f" (HT {h1.home}:{h1.away})"
        if self.status != "FT" and self.scores.full is not None:
            text += f" {self.status} {self.scores.full.home}:{self.scores.full.away}"
        return text


def _int(value: Any) -> int | None:
    # NULLs arrive from a DataFrame as None, NaN or pandas.NA.
    try:
        return None if value is None or value != value else int(value)
    except (TypeError, ValueError):
        return None


def match_record(row: Mapping[str, Any]) -> MatchRecord | None:
    """A `fact_team_match` row (lower-case keys, the HOME team's) -> MatchRecord.

    None unless the match finished. A period the source did not publish stays
    None and the engine refuses with `missing_score_component`, never a zero.
    """
    status = str(row.get("status") or "")
    if status not in STATUS_TO_ENGINE:
        return None

    def pair(for_key: str, against_key: str) -> TeamScore | None:
        home, away = _int(row.get(for_key)), _int(row.get(against_key))
        return None if home is None or away is None else TeamScore(home=home, away=away)

    corners_home, corners_away = _int(row.get("corners")), _int(row.get("corners_against"))
    return MatchRecord(
        status=status,
        scores=PeriodResolvedScores(
            h1=pair("goals_for_h1", "goals_against_h1"),
            h2=pair("goals_for_h2", "goals_against_h2"),
            reg=pair("goals_for_reg", "goals_against_reg"),
            et=pair("goals_for_et", "goals_against_et"),
            pens=pair("goals_for_pens", "goals_against_pens"),
            full=pair("goals_for_full", "goals_against_full"),
        ),
        corners=None
        if corners_home is None or corners_away is None
        else corners_home + corners_away,
        home=row.get("team_name"),
        away=row.get("opponent_name"),
    )


def _settle_corners(record: MatchRecord, period: str, side_or_line: str) -> Verdict:
    if period != "match":
        return Verdict("unsettleable", "corners_by_period_unavailable")
    if record.status != "FT":
        return Verdict("unsettleable", "corners_include_extra_time")
    if record.corners is None:
        return Verdict("unsettleable", "no_corner_statistics")
    parsed = parse_side_or_line(side_or_line)
    if parsed is None or len(parsed.line_values) != 1 or parsed.side not in ("over", "under"):
        return Verdict("unsettleable", "line_invalid")
    try:
        line = Decimal(parsed.line_values[0])
    except (InvalidOperation, ValueError):
        return Verdict("unsettleable", "line_invalid")
    total = Decimal(record.corners)
    if total == line:
        return Verdict("push")
    return Verdict("won" if (total > line) == (parsed.side == "over") else "lost")


def settle_outcome(
    record: MatchRecord, family: str, period: str, time_basis: str | None, side_or_line: str
) -> Verdict:
    """One demanded outcome against API-Football's record of the match."""
    if family in CORNER_FAMILIES:
        return _settle_corners(record, period, side_or_line)
    verdict = settle(
        record.scores,
        STATUS_TO_ENGINE[record.status],
        family,
        period,
        str(time_basis or "regular"),
        side_or_line,
    )
    return Verdict(verdict.verdict, verdict.reason)


def names_agree(record: MatchRecord, home: str | None, away: str | None) -> bool:
    return (
        similarity(home, record.home) >= CLEAR_MATCH
        and similarity(away, record.away) >= CLEAR_MATCH
    )


# fact_outcome_result, in the one order every row is written in (merge_bulk insists).
COLUMNS = (
    "event_id",
    "market_family",
    "period",
    "side_or_line",
    "time_basis",
    "verdict",
    "reason",
    "stage",
    "source",
    "score",
    "settled_at",
    "previous_verdict",
    "confirmed_at",
    "confirmed_by",
    "confirmed_score",
)


def reconcile(
    key: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
    verdict: Verdict,
    record: MatchRecord,
    names_ok: bool,
    now: datetime,
) -> dict[str, Any]:
    row = _reconcile(key, existing, verdict, record, names_ok, now)
    return {column: row.get(column) for column in COLUMNS}


def _reconcile(
    key: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
    verdict: Verdict,
    record: MatchRecord,
    names_ok: bool,
    now: datetime,
) -> dict[str, Any]:
    """The `fact_outcome_result` row after pass 2, for one settleable outcome.

    `key`: event_id, market_family, period, side_or_line (+ time_basis).
    `existing`: pass 1's row (verdict, reason, stage, source, score,
    settled_at, previous_verdict), or None.
    """
    confirmation = {
        "confirmed_at": now,
        "confirmed_by": SOURCE,
        "confirmed_score": record.score_text(),
    }
    if existing is None:
        return {
            **key,
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "stage": "confirmed",
            "source": SOURCE,
            "score": record.score_text(),
            "settled_at": now,
            "previous_verdict": None,
            **confirmation,
        }
    kept = {
        **key,
        "verdict": existing["verdict"],
        "reason": existing.get("reason"),
        "source": existing["source"],
        "score": existing.get("score"),
        "settled_at": existing["settled_at"],
        "previous_verdict": existing.get("previous_verdict"),
    }
    if existing["verdict"] == verdict.verdict:
        return {**kept, "stage": "confirmed", **confirmation}
    if not names_ok:
        # Evidence recorded, verdict untouched, stage stays open for the next run.
        return {**kept, "stage": "disputed", **confirmation, "confirmed_at": None}
    return {
        **kept,
        "verdict": verdict.verdict,
        "reason": verdict.reason,
        "previous_verdict": existing["verdict"],
        "stage": "corrected",
        **confirmation,
    }
