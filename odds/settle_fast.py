"""Pass 1 of settlement: poll msport for finished fixtures and settle what was asked about.

    demand (signals, slip legs, wallet bets) -> msport match detail -> engine
    -> core.fact_event_result, core.fact_outcome_result (stage 'provisional')

Run:
    python odds/settle_fast.py

See `arbibet_capstone.fast_settle` for why msport, and for what is refused.

DEMAND. Every (fixture, market family, period, side@line) that an EV signal, a
surebet leg, a slip leg or an open wallet bet refers to, for fixtures that
kicked off between `SETTLE_FAST_MIN_MINUTES` and `SETTLE_FAST_MAX_HOURS` ago,
have a betradar match id, and are not already settled here.

POLLING. One GET per fixture per cycle until msport says the match ended (or
was cancelled, postponed, abandoned: recorded, never settled here). At most
`SETTLE_FAST_BUDGET` requests a cycle, spaced, oldest kick-off first, so a
busy Saturday is spread over a few cycles instead of hammering the book.
A fixture whose result is stored is never asked about again.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx

from arbibet_capstone.env import load as load_env
from arbibet_capstone.fast_settle import (
    ASKED,
    HEADERS,
    SOURCE,
    fetch_msport,
    parse_msport,
    settle_demand,
    trusted,
    wallet_demand,
)
from arbibet_capstone.priority import yield_to_hot
from arbibet_capstone.warehouse import connect, merge_bulk

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("settle_fast")

MIN_MINUTES = int(os.environ.get("SETTLE_FAST_MIN_MINUTES", "105"))
MAX_HOURS = int(os.environ.get("SETTLE_FAST_MAX_HOURS", "36"))
BUDGET = int(os.environ.get("SETTLE_FAST_BUDGET", "150"))
SPACING_SECONDS = float(os.environ.get("SETTLE_FAST_SPACING", "0.4"))

_DEMAND = f"""
    WITH {ASKED}
    SELECT a.event_id, a.market_family, a.period, a.time_basis, a.side_or_line,
           f.sr_match_id, f.home_team, f.away_team, f.kickoff_at
    FROM asked a
    JOIN core.dim_fixture f ON f.event_id = a.event_id
    WHERE a.side_or_line IS NOT NULL
      AND f.sr_match_id IS NOT NULL
      AND f.kickoff_at < current_timestamp - to_minutes(%s)
      AND f.kickoff_at > current_timestamp - to_hours(%s)
      AND NOT EXISTS (
          SELECT 1 FROM core.fact_outcome_result r
          WHERE r.event_id = a.event_id AND r.market_family = a.market_family
            AND r.period = a.period AND r.side_or_line = a.side_or_line
      )
"""


def main() -> int:
    with connect() as warehouse:
        frame = warehouse.query(_DEMAND, (MIN_MINUTES, MAX_HOURS))
        fixtures: dict[str, dict[str, Any]] = {}
        demand: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in frame.itertuples(index=False):
            event_id = str(r.EVENT_ID)
            fixtures[event_id] = {
                "sr_match_id": r.SR_MATCH_ID,
                "home": r.HOME_TEAM,
                "away": r.AWAY_TEAM,
                "kickoff": r.KICKOFF_AT,
            }
            demand[event_id].append(
                {
                    "market_family": r.MARKET_FAMILY,
                    "period": r.PERIOD,
                    "time_basis": r.TIME_BASIS,
                    "side_or_line": r.SIDE_OR_LINE,
                }
            )
        for row in wallet_demand(warehouse):
            if row["event_id"] in fixtures:
                demand[row["event_id"]].append(row)

        # What the match check made of msport's listing, and fixtures flagged whole.
        checks = warehouse.query(
            "SELECT event_id, bookmaker_name, verdict, book_home, book_away "
            "FROM core.fixture_check WHERE bookmaker_name IN (%s, '*')",
            (SOURCE,),
        )
        listings: dict[str, tuple[str, str | None, str | None]] = {}
        flagged: set[str] = set()
        for r in checks.itertuples(index=False):
            if r.BOOKMAKER_NAME == "*":
                if r.VERDICT == "mismatch":
                    flagged.add(str(r.EVENT_ID))
            else:
                listings[str(r.EVENT_ID)] = (str(r.VERDICT), r.BOOK_HOME, r.BOOK_AWAY)

        stored = warehouse.query(
            "SELECT event_id, status, payload::varchar AS payload FROM core.fact_event_result "
            "WHERE source = %s",
            (SOURCE,),
        )
        known = {
            str(e): (str(s), p)
            for e, s, p in zip(stored.EVENT_ID, stored.STATUS, stored.PAYLOAD, strict=True)
        }

    log.info(
        "fixtures with unsettled demand=%d outcomes=%d",
        len(fixtures),
        sum(map(len, demand.values())),
    )
    if not fixtures:
        return 0

    def _trusted(event_id: str, parsed: Any) -> bool:
        fixture = fixtures[event_id]
        return trusted(
            parsed, fixture["home"], fixture["away"], listings.get(event_id), event_id in flagged
        )

    # --- poll: only fixtures whose final state is not stored yet ---------------------
    final = {"ended", "cancelled", "postponed", "abandoned"}
    to_poll = sorted(
        (e for e in fixtures if known.get(e, ("", None))[0] not in final),
        key=lambda e: fixtures[e]["kickoff"],
    )[:BUDGET]
    results: dict[str, Any] = {}
    polled = Counter()
    event_rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=15.0, headers=HEADERS, follow_redirects=True) as http:
        for event_id in to_poll:
            yield_to_hot()
            body = fetch_msport(http, fixtures[event_id]["sr_match_id"])
            time.sleep(SPACING_SECONDS)
            parsed = parse_msport(body) if body else None
            if parsed is None:
                polled["no answer"] += 1
                continue
            polled[parsed.status] += 1
            ok = _trusted(event_id, parsed)
            results[event_id] = (parsed, ok)
            event_rows.append(
                {
                    "event_id": event_id,
                    "source": SOURCE,
                    "status": parsed.status,
                    "raw_status": parsed.raw_status,
                    "home_ft": parsed.full_time[0] if parsed.full_time else None,
                    "away_ft": parsed.full_time[1] if parsed.full_time else None,
                    "home_ht": parsed.half_time[0] if parsed.half_time else None,
                    "away_ht": parsed.half_time[1] if parsed.half_time else None,
                    "sections": len(parsed.sections),
                    "book_home": parsed.home,
                    "book_away": parsed.away,
                    "names_ok": ok,
                    "payload": {
                        k: (body.get("data") or {}).get(k)
                        for k in (
                            "eventMatchStatus",
                            "scoreOfWholeMatch",
                            "scoreOfSection",
                            "eventMatchPeriod",
                            "homeTeam",
                            "awayTeam",
                        )
                    },
                    "fetched_at": datetime.now(UTC),
                }
            )

    # Fixtures that ended in an earlier cycle but gained new demand since.
    for event_id, (status, payload) in known.items():
        if event_id in fixtures and event_id not in results and status == "ended" and payload:
            parsed = parse_msport({"data": json.loads(payload)})
            if parsed is not None:
                results[event_id] = (parsed, _trusted(event_id, parsed))

    # --- settle ---------------------------------------------------------------------------
    outcome_rows: list[dict[str, Any]] = []
    refusals: Counter[str] = Counter()
    settled_fixtures = 0
    now = datetime.now(UTC)
    for event_id, (parsed, ok) in results.items():
        if parsed.status != "ended":
            continue
        if not ok:
            refusals["msport's teams are not verified as this fixture's"] += 1
            continue
        verdicts, refused = settle_demand(parsed, demand[event_id])
        refusals.update(refused)
        if verdicts:
            settled_fixtures += 1
        for v in verdicts:
            outcome_rows.append(
                {
                    "event_id": event_id,
                    **v,
                    "stage": "provisional",
                    "source": SOURCE,
                    "score": parsed.score_text(),
                    "settled_at": now,
                    "confirmed_at": None,
                }
            )

    with connect() as warehouse:
        if event_rows:
            merge_bulk(
                warehouse,
                table="fact_event_result",
                rows=event_rows,
                key=["event_id", "source"],
                json_columns={"payload"},
            )
        written = (
            merge_bulk(
                warehouse,
                table="fact_outcome_result",
                rows=outcome_rows,
                key=["event_id", "market_family", "period", "side_or_line"],
            )
            if outcome_rows
            else 0
        )
    log.info(
        "polled=%s settled_fixtures=%d outcomes=%d refusals=%s",
        dict(polled),
        settled_fixtures,
        written,
        dict(refusals.most_common(8)),
    )
    return written


if __name__ == "__main__":
    sys.exit(0 if main() >= 0 else 1)
