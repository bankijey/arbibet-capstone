"""Pass 2 of settlement: API-Football's results confirm, correct and extend pass 1.

    demand + pass 1's open verdicts -> core.fact_team_match -> engine
    -> core.fact_outcome_result (stage 'confirmed' | 'corrected' | 'disputed')

Run:
    python odds/settle_confirm.py

See `arbibet_capstone.confirm_settle` for what each stage means.

WHEN. API-Football's ingestor runs daily at 02:00 UTC; the cold loop flattens
its results at 04:00 and runs this right after. The warm loop runs it again
hourly, for demand that turns up later than the result did: a slip shared the
day after, a wallet bet on a fixture outside pass 1's window.

DEMAND. Everything pass 1 looks at (EV signals, surebet legs, slip legs, wallet
bets) plus every verdict pass 1 wrote that is still open, for fixtures from the
last `SETTLE_CONFIRM_MAX_DAYS` that API-Football reports as finished. An
outcome already `confirmed` or `corrected` is never looked at again, so after
the first backfill a run touches one day's fixtures. No network: this pass
reads the warehouse only.
"""

from __future__ import annotations

import logging
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from arbibet_capstone.confirm_settle import (
    match_record,
    names_agree,
    reconcile,
    settle_outcome,
)
from arbibet_capstone.env import load as load_env
from arbibet_capstone.fast_settle import ASKED, wallet_demand
from arbibet_capstone.priority import yield_to_hot
from arbibet_capstone.warehouse import connect, merge_bulk

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("settle_confirm")

MAX_DAYS = int(os.environ.get("SETTLE_CONFIRM_MAX_DAYS", "60"))

# Pass 1's rows predate `time_basis`; their family and period name it.
_WANTED = f"""
    WITH {ASKED},
    basis AS (
        SELECT market_family, period, min(time_basis) AS time_basis
        FROM core.dim_market_outcome GROUP BY 1, 2
    ),
    wanted AS (
        SELECT event_id, market_family, period, time_basis, side_or_line FROM asked
        UNION
        SELECT r.event_id, r.market_family, r.period,
               coalesce(r.time_basis, b.time_basis), r.side_or_line
        FROM core.fact_outcome_result r
        LEFT JOIN basis b ON b.market_family = r.market_family AND b.period = r.period
        WHERE r.stage NOT IN ('confirmed', 'corrected')
    )
    SELECT w.event_id, w.market_family, w.period, w.time_basis, w.side_or_line
    FROM wanted w
    JOIN core.dim_fixture f ON f.event_id = w.event_id
    WHERE w.side_or_line IS NOT NULL
      AND f.apifootball_id IS NOT NULL
      AND f.kickoff_at > current_timestamp - to_days(%s)
      AND f.kickoff_at < current_timestamp
      AND NOT EXISTS (
          SELECT 1 FROM core.fact_outcome_result r
          WHERE r.event_id = w.event_id AND r.market_family = w.market_family
            AND r.period = w.period AND r.side_or_line = w.side_or_line
            AND r.stage IN ('confirmed', 'corrected')
      )
"""

# API-Football's record of a fixture, from the FIXTURE's home team's row -- not
# `is_home`, so a tie the two sources list the other way round still reads
# home:away as the books priced it.
_MATCHES = """
    SELECT f.event_id, f.home_team, f.away_team,
           m.status, m.team_name, m.opponent_name,
           m.goals_for_h1, m.goals_against_h1, m.goals_for_h2, m.goals_against_h2,
           m.goals_for_reg, m.goals_against_reg, m.goals_for_et, m.goals_against_et,
           m.goals_for_pens, m.goals_against_pens, m.goals_for_full, m.goals_against_full,
           m.corners, o.corners AS corners_against
    FROM core.dim_fixture f
    JOIN core.fact_team_match m
      ON m.fixture_id = f.apifootball_id AND m.team_id = f.home_team_id
    LEFT JOIN core.fact_team_match o
      ON o.fixture_id = f.apifootball_id AND o.team_id = f.away_team_id
    WHERE f.apifootball_id IS NOT NULL
      AND f.kickoff_at > current_timestamp - to_days(%s)
      AND m.status IN ('FT', 'AET', 'PEN')
"""

_EXISTING = """
    SELECT event_id, market_family, period, side_or_line, verdict, reason, stage, source,
           score, settled_at, previous_verdict
    FROM core.fact_outcome_result
    WHERE stage NOT IN ('confirmed', 'corrected')
"""


def _records(frame: Any) -> list[dict[str, Any]]:
    """Rows with lower-case keys and every flavour of NULL (NaN, NaT, NA) as None."""
    return [
        {k.lower(): (None if v is None or v != v else v) for k, v in row.items()}
        for row in frame.to_dict("records")
    ]


def main() -> int:
    with connect() as warehouse:
        wanted = _records(warehouse.query(_WANTED, (MAX_DAYS,)))
        known = {
            (r["event_id"], r["market_family"], r["period"], r["side_or_line"]) for r in wanted
        }
        for row in wallet_demand(warehouse, since_days=MAX_DAYS):
            key = (row["event_id"], row["market_family"], row["period"], row["side_or_line"])
            if key not in known:
                known.add(key)
                wanted.append(row)
        matches = {str(r["event_id"]): r for r in _records(warehouse.query(_MATCHES, (MAX_DAYS,)))}
        existing = {
            (str(r["event_id"]), r["market_family"], r["period"], r["side_or_line"]): r
            for r in _records(warehouse.query(_EXISTING))
        }
        flagged = {
            str(e)
            for e in warehouse.query(
                "SELECT event_id FROM core.fixture_check "
                "WHERE bookmaker_name = '*' AND verdict = 'mismatch'"
            ).EVENT_ID
        }
        # A wallet bet's outcome may already be final: leave it alone.
        final = {
            (str(e), f, p, s)
            for e, f, p, s in warehouse.query(
                "SELECT event_id, market_family, period, side_or_line "
                "FROM core.fact_outcome_result WHERE stage IN ('confirmed', 'corrected')"
            ).itertuples(index=False)
        }

    log.info("open outcomes=%d fixtures with a result=%d", len(wanted), len(matches))
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    stages: Counter[str] = Counter()
    refusals: Counter[str] = Counter()
    waiting: set[str] = set()
    corrections: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()
    for i, want in enumerate(wanted):
        if i % 2000 == 0:
            yield_to_hot()
        event_id = str(want["event_id"])
        key = (event_id, want["market_family"], want["period"], want["side_or_line"])
        if key in seen or key in final:
            continue
        seen.add(key)
        if event_id in flagged:
            refusals["fixture flagged as a wrong match"] += 1
            continue
        match = matches.get(event_id)
        record = match_record(match) if match else None
        if record is None:
            waiting.add(event_id)
            continue
        verdict = settle_outcome(record, key[1], key[2], want.get("time_basis"), key[3])
        if verdict.verdict == "unsettleable":
            refusals[f"{key[1]}/{key[2]}: {verdict.reason}"] += 1
            continue
        before = existing.get(key)
        row = reconcile(
            {
                "event_id": event_id,
                "market_family": key[1],
                "period": key[2],
                "side_or_line": key[3],
                "time_basis": str(want.get("time_basis") or "regular"),
            },
            before,
            verdict,
            record,
            names_agree(record, match["home_team"], match["away_team"]),
            now,
        )
        stage = "new" if before is None else row["stage"]
        if before is not None and before["stage"] == "disputed" and stage == "disputed":
            continue  # nothing changed; do not rewrite it every hour
        stages[stage] += 1
        if stage in ("corrected", "disputed"):
            corrections.append(
                f"{match['home_team']} v {match['away_team']} {key[1]}/{key[2]} {key[3]}: "
                f"msport {before['verdict']} on {before.get('score')}, "
                f"API-Football {verdict.verdict} on {record.score_text()} [{stage}]"
            )
        rows.append(row)

    with connect() as warehouse:
        written = (
            merge_bulk(
                warehouse,
                table="fact_outcome_result",
                rows=rows,
                key=["event_id", "market_family", "period", "side_or_line"],
            )
            if rows
            else 0
        )
    for line in corrections[:20]:
        log.warning("pass 1 disagreed: %s", line)
    log.info(
        "written=%d %s waiting_on_apifootball=%d fixtures refusals=%s",
        written,
        dict(stages),
        len(waiting),
        dict(refusals.most_common(8)),
    )
    return written


if __name__ == "__main__":
    sys.exit(0 if main() >= 0 else 1)
