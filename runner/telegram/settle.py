"""Settle subscribers' bets from the warehouse's results. A warm job.

An open bet whose fixture kicked off more than two hours ago is looked up in
`fact_team_market_result` through `dim_market_outcome`, the same join
`gold_ev_settled` uses for the EV backtest, so a bet settles exactly as the
platform's own record of that market does. Every leg needs a verdict before
the bet settles: balances move once, and a surebet with one leg voided is
settled as what it became, not as what it promised.

Verdicts come from both settlement passes: pass 1 (msport's final score,
about two hours after kick-off) pays a bet the same evening, and pass 2
(API-Football, next morning) confirms it. If pass 2 CORRECTS a verdict a bet
was already paid on, `correct_settled_bets` re-settles the bet, moves the
difference on each book's balance and says so in the bet's note. A bet still
without a verdict a week after kick-off is marked `unsettleable` and its
stakes returned, rather than left open forever.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from arbibet_capstone.warehouse import Warehouse
from runner.observe import Run
from runner.telegram.store import Store
from runner.telegram.wallet import Leg, settle

log = logging.getLogger("runner.telegram.settle")

SETTLE_AFTER = timedelta(hours=2)
GIVE_UP_AFTER = timedelta(days=7)
# How long after kick-off a paid bet is still checked against pass 2.
CORRECT_WITHIN = timedelta(days=10)

_VERDICTS = """
    SELECT o.outcome_id,
           -- Pass 2's word is final; before it, API-Football's own table, then pass 1.
           CASE WHEN p.stage IN ('confirmed', 'corrected') THEN p.verdict
                ELSE coalesce(r.verdict, p.verdict) END AS verdict
    FROM CORE.dim_fixture f
    JOIN CORE.dim_market_outcome o ON o.market_id = %s
    LEFT JOIN CORE.fact_team_market_result r
      ON r.fixture_id    = f.apifootball_id
     AND r.team_id       = if(o.primary_team = 'away', f.away_team_id, f.home_team_id)
     AND r.market_family = o.market_family
     AND r.period        = o.period
     AND r.side_or_line  = if(o.has_line, o.side || '@' || %s, o.side)
    LEFT JOIN CORE.fact_outcome_result p
      ON p.event_id      = f.event_id
     AND p.market_family = o.market_family
     AND p.period        = o.period
     AND p.side_or_line  = if(o.has_line, o.side || '@' || %s, o.side)
    WHERE f.event_id = %s
"""


def verdicts(warehouse: Warehouse, event_id: str, market_id: str) -> dict[str, str]:
    base, _, specifier = market_id.partition(";")
    frame = warehouse.query(_VERDICTS, (base, specifier or "", specifier or "", event_id))
    return {
        str(o): str(v)
        for o, v in zip(frame.OUTCOME_ID, frame.VERDICT, strict=True)
        if isinstance(v, str)
    }


def settle_open_bets(store: Store, warehouse: Warehouse, run: Run | None = None) -> int:
    now = datetime.now(UTC)
    settled = 0
    stale = 0
    for bet in store.open_bets(now - SETTLE_AFTER):
        legs = [Leg.from_dict(leg) for leg in bet["legs"]]
        try:
            found = verdicts(warehouse, bet["event_id"], bet["market_id"])
        except Exception:
            log.warning("verdict lookup failed for bet %s", bet["bet_id"], exc_info=True)
            continue
        complete, payout = settle(legs, found)
        staked = sum(leg.stake for leg in legs)
        if not complete:
            if bet["kickoff_at"] < now - GIVE_UP_AFTER:
                for leg in legs:
                    leg.verdict, leg.payout = leg.verdict or "unsettled", leg.stake
                store.settle_bet(
                    bet["bet_id"], [leg.as_dict() for leg in legs], 0.0, "unsettleable"
                )
                _pay(store, bet, legs)
                stale += 1
            continue
        store.settle_bet(bet["bet_id"], [leg.as_dict() for leg in legs], payout - staked)
        _pay(store, bet, legs)
        settled += 1
    corrected = correct_settled_bets(store, warehouse, now)
    if run is not None:
        run.detail |= {"settled": settled, "unsettleable": stale, "corrected": corrected}
    return settled


def correct_settled_bets(store: Store, warehouse: Warehouse, now: datetime) -> int:
    """Re-pay bets whose verdict changed after they were paid (pass 2 corrected pass 1).

    Idempotent: a bet is touched only while a stored leg verdict differs from
    the warehouse's, and afterwards it does not.
    """
    corrected = 0
    for bet in store.settled_bets(now - CORRECT_WITHIN):
        old = [Leg.from_dict(leg) for leg in bet["legs"]]
        new = [Leg.from_dict(leg) for leg in bet["legs"]]
        try:
            found = verdicts(warehouse, bet["event_id"], bet["market_id"])
        except Exception:
            log.warning("verdict lookup failed for bet %s", bet["bet_id"], exc_info=True)
            continue
        complete, payout = settle(new, found)
        changed = [(a, b) for a, b in zip(old, new, strict=True) if a.verdict != b.verdict]
        if not complete or not changed:
            continue
        deltas: dict[str, float] = {}
        for a, b in zip(old, new, strict=True):
            delta = float(b.payout or 0.0) - float(a.payout or 0.0)
            if delta:
                deltas[b.book] = deltas.get(b.book, 0.0) + delta
        note = (
            "corrected on API-Football's result: "
            + "; ".join(
                f"{b.outcome or b.outcome_id} at {b.book} {a.verdict} -> {b.verdict}"
                for a, b in changed
            )
            + f" (balance {sum(deltas.values()):+,.2f})"
        )
        store.correct_bet(
            bet["bet_id"],
            [leg.as_dict() for leg in new],
            payout - sum(leg.stake for leg in new),
            note,
        )
        if deltas:
            store.move_balances(int(bet["chat_id"]), bet["mode"], deltas)
        log.warning("bet %s %s", bet["bet_id"], note)
        corrected += 1
    return corrected


def _pay(store: Store, bet: dict[str, Any], legs: list[Leg]) -> None:
    deltas: dict[str, float] = {}
    for leg in legs:
        deltas[leg.book] = deltas.get(leg.book, 0.0) + float(leg.payout or 0.0)
    if deltas:
        store.move_balances(int(bet["chat_id"]), bet["mode"], deltas)
