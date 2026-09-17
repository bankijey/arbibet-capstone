"""What an alert is, and whether a subscriber should get one. Pure, so it is tested.

An OPPORTUNITY is what the hot loop found, named for a reader: a true surebet
on a market (every leg priced within five minutes, arbitrage above 1) or a
positive-EV price on one outcome at one book.

The LEDGER decides who hears about it. The hot loop recomputes a fixture every
time any book re-publishes it, so the same surebet is "found" dozens of times
an hour; a subscriber is told once per market (surebet) or per outcome at a
book (EV), and again only when the value improves materially -- the step
below. A value that falls and recovers to where it was is not news.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# How much better an already-alerted opportunity must get to be sent again.
# Absolute, in the value's own units: arbitrage 1.012 -> 1.017, EV 0.03 -> 0.05.
SUREBET_STEP = 0.005
EV_STEP = 0.02


@dataclass(frozen=True)
class Leg:
    outcome: str
    book: str
    odds: float
    url: str | None = None


@dataclass(frozen=True)
class Opportunity:
    kind: str  # "surebet" | "ev"
    event_id: str
    market_id: str
    fixture: str
    tournament: str | None
    kickoff: datetime
    market: str  # name, with the line when there is one
    value: float  # arbitrage (surebet) or EV per unit staked (ev)
    legs: tuple[Leg, ...]
    detected_at: datetime
    spread_seconds: int | None = None
    probability: float | None = None
    p_source: str | None = None
    outcome_id: str | None = None

    @property
    def key(self) -> str:
        """What a subscriber is told about once: the market, or the outcome at a book."""
        if self.kind == "surebet":
            return f"surebet|{self.event_id}|{self.market_id}"
        leg = self.legs[0]
        return f"ev|{self.event_id}|{self.market_id}|{self.outcome_id}|{leg.book}"

    def books(self) -> set[tuple[str, str, str]]:
        """(event, market, book) per leg: the grain a "not on site" flag is kept at."""
        return {(self.event_id, self.market_id, leg.book) for leg in self.legs}


def best_per_key(opportunities: Iterable[Opportunity]) -> list[Opportunity]:
    """One opportunity per key, the highest value. A recompute can yield several
    leg combinations for one market; the reader needs the best of them."""
    best: dict[str, Opportunity] = {}
    for opp in opportunities:
        current = best.get(opp.key)
        if current is None or opp.value > current.value:
            best[opp.key] = opp
    return sorted(best.values(), key=lambda o: (o.kind, -o.value, o.key))


@dataclass
class Subscriber:
    chat_id: int
    username: str | None = None
    active: bool = True
    surebets: bool = True
    ev: bool = True
    ev_min: float = 0.015
    stake: float = 100.0
    muted_until: datetime | None = None

    def muted(self, now: datetime) -> bool:
        return self.muted_until is not None and self.muted_until > now

    def wants(self, opp: Opportunity, now: datetime) -> bool:
        if not self.active or self.muted(now):
            return False
        if opp.kind == "surebet":
            return self.surebets
        return self.ev and opp.value >= self.ev_min


@dataclass
class Ledger:
    """(chat_id, key) -> the value last alerted."""

    sent: dict[tuple[int, str], float] = field(default_factory=dict)
    surebet_step: float = SUREBET_STEP
    ev_step: float = EV_STEP

    def due(self, chat_id: int, opp: Opportunity) -> bool:
        last = self.sent.get((chat_id, opp.key))
        if last is None:
            return True
        step = self.surebet_step if opp.kind == "surebet" else self.ev_step
        return opp.value >= last + step - 1e-9

    def mark(self, chat_id: int, opp: Opportunity) -> None:
        key = (chat_id, opp.key)
        self.sent[key] = max(opp.value, self.sent.get(key, opp.value))


def eligible(
    opp: Opportunity,
    now: datetime,
    flagged: set[tuple[str, str, str]],
    ev_max: float,
) -> bool:
    """Whether an opportunity may be alerted to anyone at all.

    Never after kick-off, never on a leg a viewer marked "not on site", never a
    "surebet" that is not one, and never an EV so large it is almost certainly
    a mispriced or mis-mapped market rather than an edge.
    """
    if opp.kickoff <= now:
        return False
    if opp.books() & flagged:
        return False
    if opp.kind == "surebet":
        return opp.value > 1.0 and len({leg.book for leg in opp.legs}) >= 2
    return 0 < opp.value <= ev_max


def recipients(
    opp: Opportunity,
    subscribers: Iterable[Subscriber],
    ledger: Ledger,
    now: datetime,
) -> list[Subscriber]:
    return [s for s in subscribers if s.wants(opp, now) and ledger.due(s.chat_id, opp)]


def split_stake(odds: list[float], stake: float) -> dict[str, Any] | None:
    """The dashboard's sizing arithmetic: equal return whichever outcome lands."""
    if not odds or any(o is None or o <= 1 for o in odds) or stake <= 0:
        return None
    inverse = [1.0 / o for o in odds]
    total = sum(inverse)
    arbitrage = 1.0 / total
    stakes = [stake * i / total for i in inverse]
    return {
        "arbitrage": arbitrage,
        "stakes": stakes,
        "returns": stake * arbitrage,
        "profit": stake * arbitrage - stake,
    }


def outcome_names(markets_by_book: Mapping[str, Iterable[Any]]) -> dict[tuple[str, str], str]:
    """(market_id, outcome_id) -> a name any book gave it."""
    names: dict[tuple[str, str], str] = {}
    for markets in markets_by_book.values():
        for market in markets:
            for outcome in market.outcomes:
                if outcome.name and (market.market_id, outcome.id) not in names:
                    names[(market.market_id, outcome.id)] = str(outcome.name)
    return names
