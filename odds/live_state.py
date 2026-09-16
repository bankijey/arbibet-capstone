"""What the books say right now: match status, and whether a signal's leg is still offered.

    bronze latest payload per book -> fact_event_state, fact_leg_availability

Run:
    python odds/live_state.py

Two questions the rest of the warehouse cannot answer, because everything else
is built from what was PRICED, not from what is on the site now.

1. MATCH STATUS for every fixture on a booking slip that has kicked off in the
   last 36 hours, or is about to: not started, first half, ended, with the
   score and minute. From msport's payload -- the one book whose response
   carries it (`eventMatchStatus`, `scoreOfWholeMatch`, `playedTime`) -- which
   bronze keeps polling through full time. A slip's own payload cannot do this:
   finished legs drop off the slip.

2. AVAILABILITY of every leg of an upcoming signal -- the legs of every market
   that ever carried a fresh surebet, and every fresh positive-EV row: is the
   outcome in the book's latest payload, parsed through the same crosswalk the
   detector uses, and at what price. A surebet on Seravezza v Montevarchi was
   still listed after msport had removed the whole Over/Under market from its
   response; the link opened a page without it.

Writes only rows whose state changed since the last run.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from arbibet_capstone import bronze
from arbibet_capstone.env import load as load_env
from arbibet_capstone.fixtures import Fixture
from arbibet_capstone.signals import MAX_LEG_SPREAD_SECONDS
from arbibet_capstone.snapshot import build
from arbibet_capstone.warehouse import bookmaker_ids, connect, merge_bulk

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("live_state")

STATE_BOOK = "msport"

_SLIP_FIXTURES = """
    SELECT DISTINCT event_id
    FROM ANALYTICS.gold_slip_leg_history
    WHERE kickoff_at BETWEEN dateadd(hour, -36, current_timestamp())
                         AND dateadd(hour, 3, current_timestamp())
"""

# Legs to check: every leg ever used by a fresh surebet on a fixture still to
# be played, and every fresh positive-EV outcome on one.
_SIGNAL_LEGS = f"""
    SELECT DISTINCT l.event_id, l.market_id, l.outcome_id, l.bookmaker_name
    FROM ANALYTICS.stg_arbitrage_leg l
    JOIN CORE.dim_fixture f ON f.event_id = l.event_id
    WHERE l.arbitrage > 1 AND l.spread_seconds <= {MAX_LEG_SPREAD_SECONDS}
      AND f.kickoff_at > current_timestamp()
    UNION
    SELECT DISTINCT s.event_id, s.market_id, s.outcome_id, s.bookmaker_name
    FROM ANALYTICS.stg_ev_signal s
    JOIN CORE.dim_fixture f ON f.event_id = s.event_id
    WHERE s.ev > 0 AND s.is_fresh AND f.kickoff_at > current_timestamp()
"""


def _event_state(payload: bytes) -> dict[str, Any] | None:
    data = json.loads(payload).get("data") or {}
    status = data.get("eventMatchStatus")
    if status is None:
        return None
    return {
        "match_status": str(status),
        "score": data.get("scoreOfWholeMatch"),
        "played_time": data.get("playedTime"),
    }


def main() -> int:
    now = datetime.now(UTC)
    with connect() as warehouse, warehouse.cursor() as cur:
        cur.execute(_SLIP_FIXTURES)
        slip_events = {str(e) for (e,) in cur.fetchall()}
        cur.execute(_SIGNAL_LEGS)
        legs: dict[str, set[tuple[str, str, str]]] = {}
        for e, m, o, b in cur.fetchall():
            legs.setdefault(str(e), set()).add((str(m), str(o), str(b)))
        cur.execute("SELECT event_id, match_status, score, played_time FROM CORE.fact_event_state")
        stored_state = {str(e): (s, sc, p) for e, s, sc, p in cur.fetchall()}
        cur.execute(
            "SELECT event_id, market_id, outcome_id, bookmaker_id, offered, current_odds "
            "FROM CORE.fact_leg_availability"
        )
        stored_legs = {
            (str(e), str(m), str(o), int(b)): (bool(off), None if odds is None else float(odds))
            for e, m, o, b, off, odds in cur.fetchall()
        }
        books = bookmaker_ids(warehouse)

    states: list[dict[str, Any]] = []
    availability: list[dict[str, Any]] = []
    with bronze.connect() as source:
        for event_id in sorted(slip_events | set(legs)):
            payloads = bronze.latest_payloads(source, UUID(event_id))

            if event_id in slip_events and STATE_BOOK in payloads:
                state = _event_state(payloads[STATE_BOOK].payload)
                if state and stored_state.get(event_id) != (
                    state["match_status"], state["score"], state["played_time"]
                ):
                    states.append(
                        {
                            "event_id": event_id,
                            **state,
                            "source_book": STATE_BOOK,
                            "fired_at": payloads[STATE_BOOK].fire_time,
                            "checked_at": now,
                        }
                    )

            if event_id not in legs:
                continue
            wanted_books = {b for _, _, b in legs[event_id]}
            snapshot = build(
                Fixture(UUID(event_id), now, None, None, None, None, None, None, None),
                {b: p for b, p in payloads.items() if b in wanted_books},
            )
            prices = {
                (book, market.market_id, outcome.id): outcome.odds
                for book, quote in snapshot.books.items()
                for market in quote.markets
                for outcome in market.outcomes
                if outcome.odds > 1
            }
            for market_id, outcome_id, book in legs[event_id]:
                if book not in books:
                    continue
                odds = prices.get((book, market_id, outcome_id))
                key = (event_id, market_id, outcome_id, books[book])
                if stored_legs.get(key) == (odds is not None, odds):
                    continue
                availability.append(
                    {
                        "event_id": event_id,
                        "market_id": market_id,
                        "outcome_id": outcome_id,
                        "bookmaker_id": books[book],
                        "offered": odds is not None,
                        "current_odds": odds,
                        "book_fired_at": payloads[book].fire_time if book in payloads else None,
                        "checked_at": now,
                    }
                )

    with connect() as warehouse:
        n_states = (
            merge_bulk(warehouse, table="fact_event_state", rows=states, key=["event_id"])
            if states
            else 0
        )
        n_legs = (
            merge_bulk(
                warehouse,
                table="fact_leg_availability",
                rows=availability,
                key=["event_id", "market_id", "outcome_id", "bookmaker_id"],
            )
            if availability
            else 0
        )
    log.info(
        "slip fixtures=%d signal legs=%d states written=%d legs written=%d (withdrawn now=%d)",
        len(slip_events), sum(len(v) for v in legs.values()), n_states, n_legs,
        sum(1 for r in availability if not r["offered"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
