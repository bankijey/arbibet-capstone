"""The bet wallet's arithmetic. Pure, so it is tested.

A subscriber keeps a BALANCE per bookmaker, in two modes: `real` (money at
the book) and `paper` (a practice wallet that starts at PAPER_START per book).
A BET is one placed opportunity: for a surebet, one leg per outcome at its
book; for an EV bet, one leg. Placing moves stake out of each leg's balance;
settling pays each leg back at its verdict.

SIZING. The dashboard's stake split says what fraction of a total to put on
each leg. Here the total is chosen from what the subscriber actually holds:
the largest total whose every leg fits its book's balance, capped by the
subscriber's default stake. The binding book is reported, because "you could
have taken 1.9% on 30,000 but livescorebet held 4,700" is the useful sentence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

PAPER_START = 100_000.0
# A surebet's return once a leg is voided is no longer guaranteed; a pushed or
# voided leg returns its stake, as at the books.
PAYOUT = {
    "won": lambda stake, odds: stake * odds,
    "lost": lambda stake, odds: 0.0,
    "push": lambda stake, odds: stake,
    "void": lambda stake, odds: stake,
    "half_win": lambda stake, odds: stake + stake * (odds - 1) / 2,
    "half_loss": lambda stake, odds: stake / 2,
}


@dataclass
class Sizing:
    total: float
    stakes: list[float]
    arbitrage: float | None  # surebets only
    limited_by: str | None  # the book whose balance capped the total, if any
    short: list[str] = field(default_factory=list)  # books with no balance at all


def fractions(odds: Sequence[float]) -> list[float] | None:
    if not odds or any(o is None or o <= 1 for o in odds):
        return None
    inverse = [1.0 / o for o in odds]
    total = sum(inverse)
    return [i / total for i in inverse]


def size_surebet(
    odds: Sequence[float], books: Sequence[str], balances: Mapping[str, float], cap: float
) -> Sizing | None:
    """The largest total, up to `cap`, whose every leg fits its book's balance."""
    parts = fractions(odds)
    if parts is None:
        return None
    arbitrage = 1.0 / sum(1.0 / o for o in odds)
    short = [b for b in books if balances.get(b, 0.0) <= 0]
    total, limited_by = cap, None
    for book, part in zip(books, parts, strict=True):
        allowed = balances.get(book, 0.0) / part if part > 0 else cap
        if allowed < total:
            total, limited_by = allowed, book
    total = max(0.0, total)
    return Sizing(total, [total * p for p in parts], arbitrage, limited_by, short)


def size_ev(
    ev: float, odds: float, book: str, balances: Mapping[str, float], bankroll: float, cap: float
) -> Sizing | None:
    """Quarter-Kelly of the bankroll, no more than the book holds or the cap."""
    if odds <= 1 or ev <= 0:
        return None
    kelly = ev / (odds - 1) / 4
    stake = min(cap, bankroll * kelly)
    limited_by = None
    if balances.get(book, 0.0) < stake:
        stake, limited_by = balances.get(book, 0.0), book
    return Sizing(stake, [stake], None, limited_by, [book] if balances.get(book, 0.0) <= 0 else [])


@dataclass
class Leg:
    book: str
    outcome_id: str
    outcome: str
    odds: float
    stake: float
    verdict: str | None = None
    payout: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "book": self.book,
            "outcomeId": self.outcome_id,
            "outcome": self.outcome,
            "odds": self.odds,
            "stake": self.stake,
            "verdict": self.verdict,
            "payout": self.payout,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Leg:
        return cls(
            d["book"],
            str(d["outcomeId"]),
            d.get("outcome") or "",
            float(d["odds"]),
            float(d["stake"]),
            d.get("verdict"),
            d.get("payout"),
        )


def guaranteed(legs: Sequence[Leg]) -> float | None:
    """A surebet's return whichever outcome lands -- the minimum over legs -- or
    None when the legs do not cover every outcome equally (an EV bet)."""
    if len(legs) < 2:
        return None
    return min(leg.stake * leg.odds for leg in legs)


def settle(legs: Sequence[Leg], verdicts: Mapping[str, str]) -> tuple[bool, float]:
    """Apply verdicts keyed by outcome id. Returns (complete, total payout).

    Incomplete when any leg has no verdict yet: a bet settles whole, never
    leg by leg, so balances move once.
    """
    total = 0.0
    for leg in legs:
        verdict = verdicts.get(leg.outcome_id)
        if verdict not in PAYOUT:
            return False, 0.0
        leg.verdict = verdict
        leg.payout = PAYOUT[verdict](leg.stake, leg.odds)
        total += leg.payout
    return True, total


def summary(bets: Sequence[Mapping[str, Any]], balances: Mapping[str, float]) -> dict[str, Any]:
    """Equity, open exposure, locked-in profit and settled P&L for one wallet mode."""
    open_bets = [b for b in bets if b["status"] == "open"]
    settled = [b for b in bets if b["status"] == "settled"]
    staked_open = sum(leg["stake"] for b in open_bets for leg in b["legs"])
    locked = 0.0
    for b in open_bets:
        legs = [Leg.from_dict(leg) for leg in b["legs"]]
        sure = guaranteed(legs)
        if b["kind"] == "surebet" and sure is not None:
            locked += sure - sum(leg.stake for leg in legs)
    staked_settled = sum(leg["stake"] for b in settled for leg in b["legs"])
    profit = sum(float(b.get("profit") or 0.0) for b in settled)
    won = sum(1 for b in settled if float(b.get("profit") or 0.0) > 0)
    cash = sum(balances.values())
    return {
        "cash": cash,
        "open_bets": len(open_bets),
        "open_stake": staked_open,
        "locked_profit": locked,
        "equity": cash + staked_open,
        "settled_bets": len(settled),
        "settled_won": won,
        "settled_stake": staked_settled,
        "profit": profit,
        "roi": profit / staked_settled if staked_settled else None,
    }


def parse_stakes(text: Sequence[str], legs: int) -> list[float] | None:
    """`/placed 4700 5000` -> stakes in leg order; None when malformed."""
    try:
        values = [float(t.replace(",", "")) for t in text]
    except ValueError:
        return None
    if len(values) != legs or any(v < 0 for v in values):
        return None
    return values


def bet_row(
    chat_id: int,
    mode: str,
    kind: str,
    opp: Any,
    legs: Sequence[Leg],
    placed_at: datetime,
    source: str,
) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "mode": mode,
        "kind": kind,
        "opportunity_key": opp.key,
        "event_id": opp.event_id,
        "market_id": opp.market_id,
        "fixture": opp.fixture,
        "market": opp.market,
        "kickoff_at": opp.kickoff,
        "placed_at": placed_at,
        "source": source,
        "legs": [leg.as_dict() for leg in legs],
    }
