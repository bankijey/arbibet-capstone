"""Turning one fixture's market and history into a prose brief an LLM can write.

The prompt is built here, apart from the API call, because the prompt is the
part with decisions in it. This one has three, and all three exist to stop the
model saying something the warehouse cannot support.

**The model may use NOTHING but what it is given.** A football fixture is
exactly the sort of subject a language model already has opinions about: it
knows Bayern are strong, it may remember a result, it will happily supply a
league position nobody measured. Every such sentence would be unverifiable
against this warehouse and indistinguishable from the parts that are. The
system prompt forbids outside knowledge explicitly and the user prompt carries
every number the answer may contain.

**The market and the record are different kinds of claim.** A price is what a
bookmaker asserts; a settled rate is what actually happened. The brief is asked
to keep them apart rather than average them into a verdict, because the gap
between them is the only interesting thing on the page.

**Recent form is not a probability.** Ten matches, opponent unknown. The
prompt says so in those terms -- the same instruction the slip summariser
needs, for the same reason.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, NamedTuple

MODEL = "gpt-4o-mini"
# How many recent matches the form lines are drawn from.
FORM_WINDOW_LABEL = 10

_SYSTEM = """You write short factual briefs on football fixtures for a sports \
data platform.

ABSOLUTE RULE: use ONLY the numbers given to you in the message. You have no \
other information about these teams, this competition or this fixture. Do not \
mention league position, injuries, transfers, managers, historical rivalries, \
past meetings, or any result that is not in the data. If something is not in \
the data, it does not go in the brief. Never say "reportedly", "traditionally" \
or "as expected" -- you have no basis for any of them.

How to read what you are given:

`market` lines are BOOKMAKER PRICES: what the books were charging. A price is \
a claim, not an outcome. Where books disagree, say so plainly and say by how \
much -- that disagreement is the platform's whole subject.

`form` lines are SETTLED RESULTS from match history, over at most ten matches \
each, against unknown opposition. This is RECENT FORM, never a probability for \
this fixture. Never call a price wrong on the strength of it.

COMPETITION MATTERS. The header names the competition this fixture is in, \
and `form` lines are split by the competition each record was earned in. A \
side's record in the SAME competition is the relevant evidence. A record \
from a different competition is weaker: a cup tie, above all an early round, \
can be against much weaker or much stronger opposition than a league match, \
and a friendly means little. When you use a record, say which competition it \
comes from. Never pool a cup record with a league record as if they were the \
same evidence. You do not know league positions or how strong any opponent \
was: do not guess them.

`settled` lines are how often a specific market actually landed for that side \
over the same window. Same caveat.

Write three short paragraphs, plain and specific, no headings and no bullet \
points:

1. What the market thought, including where the books disagreed and how the \
price moved.
2. What both sides had actually been doing, with the numbers.
3. Where the market and the record point the same way and where they do not. \
If the data is too thin to say, say that instead -- it is a useful answer.

`signal` lines are what OUR platform detected on this fixture, not \
something a book advertises: an arbitrage above 1.0 is a guaranteed \
return if every leg is staked at the prices shown, and a positive EV is \
a price longer than the probability we attach to it. Mention a signal \
ONLY if a `signal` line appears below. Never say a signal is small, \
large or reliable beyond what its number says, and never describe one \
as a prediction -- an arbitrage is a hedge and does not care who wins."""


# --- recompute thresholds --------------------------------------------------
#
# A brief costs an LLM call and a warehouse write, and the DAG runs every
# thirty minutes. So the question each run asks is not "did anything change"
# but "did anything change ENOUGH", and these are the two numbers that answer
# it. Both are measured on our own ticks, not chosen by feel.
#
# PRICE_BAND = 0.02 of implied probability. Across 156,171 recorded repricings
# the median move is 0.009 and 79.6% are under 0.02 (p75 0.0172, p90 0.0346).
# The guess as to why the distribution sits there: books quote on a discrete
# ladder whose step near even money is 0.05 of decimal odds -- 1.85 to 1.90 is
# 1.4 points of probability -- so most recorded "moves" are a single rung, and
# a threshold just above one rung ignores a book shading a price while firing
# on a book changing its mind.
#
# ARB_STEP = 0.01 of the ratio. Of the arbitrage re-detections that moved at
# all, 45.7% moved less than this (median 0.0107). The guess: real arbitrage
# lives in 1.00-1.02, so one point of ratio is most of the distance between
# "worth the stake" and "gone once commission is paid". A smaller move does
# not change the advice, and the brief exists to give advice.
#
# WHY THIS IS A COMPARISON AND NOT A ROUNDING
# -------------------------------------------
# The obvious implementation is to round each number to the threshold and hash
# the result -- no stored state, one line. It does not work, and the failure is
# quiet. A fixed lattice converts a move of size d into a rewrite with
# probability d/w: measured on the same 156,171 repricings, banding to 0.02
# leaves only 46.7% of moves in the same band. Half of all ticks would still
# trigger a rewrite, most of them movements of a fifth of a point of
# probability, and the log would report the thresholds working.
#
# So the previous brief's numbers are STORED and the next run compares against
# them. That is a threshold on change, which is what a threshold on change has
# to be.
#
# WHAT IS DELIBERATELY NOT THRESHOLDED -- these rewrite at any size:
# a signal appearing where there was none, a signal disappearing, a change in
# how many legs carry it or in WHICH books do, and any change to the form or
# settled-rate evidence. Those are changes of state, not of degree, and a
# threshold that swallowed them would leave a page telling a reader an
# arbitrage is still there after it has gone.
PRICE_BAND = 0.02
ARB_STEP = 0.01


class MarketLine(NamedTuple):
    """One market's price picture across the books."""

    market: str
    outcome: str
    books: int
    opening: float | None
    latest: float | None
    lowest: float | None
    highest: float | None
    changes: int


class TeamForm(NamedTuple):
    """One side's settled record over the form window."""

    team: str
    role: str
    matches: int
    wins: int
    draws: int
    losses: int
    goals_for: float | None
    goals_against: float | None
    xg: float | None
    corners: float | None
    # The competition the record was earned in. Defaulted so a brief built
    # without it -- every caller before it existed -- still constructs.
    competition: str | None = None


class SettledRate(NamedTuple):
    """How often one market actually landed for one side."""

    team: str
    market: str
    period: str
    pick: str
    landed: int
    matches: int


class LiveSignal(NamedTuple):
    """An opportunity standing on this fixture right now."""

    kind: str  # "arbitrage" or "positive EV"
    market: str
    outcome: str | None
    value: float  # arbitrage ratio, or EV
    legs: int
    books: str  # the masked books carrying it, comma-joined


class FixtureBrief(NamedTuple):
    """Everything the model is allowed to know about one fixture."""

    event_id: str
    home_team: str
    away_team: str
    tournament: str | None
    kickoff: str
    markets: list[MarketLine]
    form: list[TeamForm]
    settled: list[SettledRate]
    # Defaulted so a brief built without signals -- every caller before signals
    # existed -- still constructs. A tuple, because a NamedTuple default is shared.
    signals: Sequence[LiveSignal] = ()

    def signature(self) -> str:
        """Identity of the EVIDENCE, not of the fixture.

        The same lesson as `Slip.signature`: a brief describes numbers that
        move independently of the thing they describe. Prices tick, history
        settles, a fixture's team ids resolve or stop resolving. Hashing the
        fixture id alone would let a brief outlive every number in it -- which
        is exactly how a slip summary came to sit on the dashboard citing form
        of 90% beside a table reading "no history".
        """
        material = "|".join(
            [
                self.event_id,
                *(f"{m.market}:{m.outcome}:{m.latest}" for m in self.markets),
                *(
                    f"{f.team}:{f.competition}:{f.wins}/{f.matches}:{f.goals_for}"
                    for f in self.form
                ),
                *(f"{s.team}:{s.market}:{s.pick}:{s.landed}/{s.matches}" for s in self.settled),
                *(
                    f"{g.kind}:{g.market}:{g.outcome}:{g.value}:{g.legs}:{g.books}"
                    for g in self.signals
                ),
            ]
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def evidence(self) -> dict[str, Any]:
        """The numbers the thresholds compare, split by how they are compared.

        `state` is everything that rewrites at ANY size, collapsed into one
        comparable structure. `numbers` is everything a threshold applies to,
        keyed so the next run can line them up one by one.
        """
        return {
            "state": {
                "signals": sorted(
                    f"{g.kind}|{g.market}|{g.outcome}|{g.legs}|{g.books}" for g in self.signals
                ),
                "form": sorted(
                    f"{f.team}|{f.competition}|{f.wins}/{f.draws}/{f.losses} of {f.matches}"
                    for f in self.form
                ),
                "settled": sorted(
                    f"{s.team}|{s.market} {s.period} {s.pick}|{s.landed}/{s.matches}"
                    for s in self.settled
                ),
                "markets": sorted(f"{m.market}|{m.outcome}" for m in self.markets),
            },
            "numbers": {
                **{f"price|{m.market}|{m.outcome}": _implied(m.latest) for m in self.markets},
                **{
                    f"{'arb' if g.kind == 'arbitrage' else 'ev'}|{g.market}|{g.outcome}": g.value
                    for g in self.signals
                },
            },
        }


def _implied(odds: float | None) -> float | None:
    """Decimal odds as a probability.

    The threshold is stated in probability rather than in odds because a fixed
    odds band is a different amount of information at each end of the book:
    0.05 is three points of probability at 1.30 and a third of one point at
    12.0. Comparing prices directly would ignore real moves on favourites and
    fire constantly on outsiders.
    """
    return None if odds is None or odds <= 1 else 1.0 / odds


def _moved(before: object, after: object, threshold: float) -> bool:
    """Did a stored number move past its threshold?

    A number appearing or disappearing counts as movement at any size: it is a
    change of state, and the whole point of the not-thresholded list above.
    """
    if before is None or after is None:
        return before is not after
    if isinstance(before, int | float) and isinstance(after, int | float):
        return abs(float(after) - float(before)) >= threshold
    # Not numbers at all. Compared exactly rather than guessed at: a threshold
    # that quietly treats an unparseable value as "unchanged" is how a page
    # stops updating for a reason nobody can see.
    return before != after


def needs_rewrite(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    """Whether this brief has moved enough to be worth writing again.

    `previous` is the `evidence` of the last brief stored for this fixture, or
    None when there is none. Anything unreadable rewrites -- a threshold that
    cannot read its own history has to fail towards being correct rather than
    towards being cheap.
    """
    if not previous:
        return True
    if previous.get("state") != current.get("state"):
        return True  # a signal, a book, a leg count or the history changed

    before = previous.get("numbers") or {}
    after = current.get("numbers") or {}
    if set(before) != set(after):
        return True  # a market or signal appeared or went away

    return any(
        _moved(before[k], after[k], ARB_STEP if k.startswith(("arb|", "ev|")) else PRICE_BAND)
        for k in after
    )


def _market_line(m: MarketLine) -> str:
    def price(value: float | None) -> str:
        return f"{value:.2f}" if value is not None else "?"

    return (
        f"- market | {m.market} | {m.outcome} | {m.books} books "
        f"| opened {price(m.opening)} | latest {price(m.latest)} "
        f"| range {price(m.lowest)}-{price(m.highest)} "
        f"| {m.changes} price changes"
    )


def _form_line(f: TeamForm) -> str:
    def stat(value: float | None, label: str) -> str:
        return f" | {label} {value:.2f}" if value is not None else ""

    return (
        f"- form | {f.team} ({f.role}) | {f.competition or 'competition not recorded'} "
        f"| {f.matches} of last {FORM_WINDOW_LABEL}: "
        f"{f.wins}W {f.draws}D {f.losses}L"
        + stat(f.goals_for, "scored/match")
        + stat(f.goals_against, "conceded/match")
        + stat(f.xg, "xG/match")
        + stat(f.corners, "corners/match")
    )


def _settled_line(s: SettledRate) -> str:
    rate = s.landed / s.matches if s.matches else 0.0
    return (
        f"- settled | {s.team} | {s.market} {s.period} {s.pick} | "
        f"landed {s.landed}/{s.matches} ({rate:.0%})"
    )


def _signal_line(g: LiveSignal) -> str:
    outcome = f" {g.outcome}" if g.outcome else ""
    return (
        f"- signal | {g.kind} | {g.market}{outcome} | {g.value:.4f} "
        f"| {g.legs} legs across {g.books}"
    )


def build_prompt(brief: FixtureBrief) -> list[dict[str, str]]:
    """The messages for one fixture."""
    header = (
        f"{brief.home_team} v {brief.away_team}"
        f" | {brief.tournament or 'competition not recorded'}"
        f" | kick-off {brief.kickoff} (Europe/Berlin)"
    )

    lines = [
        *(_signal_line(g) for g in brief.signals),
        *(_market_line(m) for m in brief.markets),
        *(_form_line(f) for f in brief.form),
        *(_settled_line(s) for s in brief.settled),
    ]
    if not lines:
        # Said explicitly rather than sending an empty prompt: a model handed a
        # fixture name and nothing else writes a brief entirely out of its own
        # memory, which is the one outcome this whole module exists to prevent.
        lines = ["(no market or history data is available for this fixture)"]

    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": header + "\n\n" + "\n".join(lines)},
    ]


def brief_from_rows(
    fixture: Any,
    markets: list[dict[str, Any]],
    form: list[dict[str, Any]],
    settled: list[dict[str, Any]],
    signals: list[dict[str, Any]] | None = None,
) -> FixtureBrief:
    """Assemble a brief from warehouse rows, keyed by Snowflake's UPPER names."""
    return FixtureBrief(
        event_id=str(fixture["EVENT_ID"]),
        home_team=str(fixture["HOME_TEAM"]),
        away_team=str(fixture["AWAY_TEAM"]),
        tournament=fixture["TOURNAMENT"],
        kickoff=f"{fixture['KICKOFF_AT']:%A %d %B %Y, %H:%M}",
        markets=[
            MarketLine(
                market=str(r["MARKET"]),
                outcome=str(r["OUTCOME"]),
                books=int(r["BOOKS"]),
                opening=r["OPENING"],
                latest=r["LATEST"],
                lowest=r["LOWEST"],
                highest=r["HIGHEST"],
                changes=int(r["CHANGES"]),
            )
            for r in markets
        ],
        form=[
            TeamForm(
                team=str(r["TEAM"]),
                role=str(r["ROLE"]),
                matches=int(r["MATCHES"]),
                wins=int(r["WINS"]),
                draws=int(r["DRAWS"]),
                losses=int(r["LOSSES"]),
                goals_for=r["GOALS_FOR"],
                goals_against=r["GOALS_AGAINST"],
                xg=r["XG"],
                corners=r["CORNERS"],
                competition=r.get("COMPETITION"),
            )
            for r in form
        ],
        settled=[
            SettledRate(
                team=str(r["TEAM"]),
                market=str(r["MARKET_FAMILY"]),
                period=str(r["PERIOD"]),
                pick=str(r["SIDE_OR_LINE"]),
                landed=int(r["LANDED"]),
                matches=int(r["MATCHES"]),
            )
            for r in settled
        ],
        signals=[
            LiveSignal(
                kind=str(r["KIND"]),
                market=str(r["MARKET"]),
                outcome=r["OUTCOME"],
                value=float(r["VALUE"]),
                legs=int(r["LEGS"]),
                books=str(r["BOOKS"]),
            )
            for r in (signals or [])
        ],
    )
