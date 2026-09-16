"""The prompt for a finished match: what the market expected, what happened.

Written once per fixture and never revised, so unlike the slip and fixture
prompts this one has no signature to track -- a settled result does not move.

The interesting content is the GAP: the books priced this at 1.85 and it lost;
we flagged an arbitrage and here is whether the leg that carried it landed.
That comparison is only honest if both halves come from our own tables, so the
prompt carries the pre-kickoff prices we recorded and the post-match verdicts
the settlement engine produced, and forbids everything else.

The model is told, in particular, not to explain WHY a result happened. We have
no team news, no injuries, no lineups and no commentary -- a model asked why
Osnabruck lost will invent a red card. It may describe what the market thought
and what the scoreline was; it may not narrate a match it cannot see.
"""

from __future__ import annotations

from typing import Any, NamedTuple

MODEL = "gpt-4o-mini"

_SYSTEM = """You write short factual notes on football matches that have \
already finished, for a sports data platform.

ABSOLUTE RULE: use ONLY the numbers in the message. You have no other knowledge \
of this match. Do not describe goals, cards, substitutions, injuries, tactics, \
momentum or crowd -- you were not shown any of that and you cannot infer it \
from a scoreline. Do not name a scorer. Do not explain WHY the result happened.

What you are shown:

`final` is the settled score and the match statistics we recorded.

COMPETITION MATTERS. The header names the competition. Say it, and judge \
whether the market had the match right WITHIN that competition: an early cup \
round is not a league fixture, and a result in one should not be described \
by the standards of the other. You do not know league positions or how \
strong either side is beyond the prices shown: do not guess them.

`market` lines are what the bookmakers were charging BEFORE kick-off, and \
whether that market ultimately landed. A price is what the books believed; the \
verdict is what happened. The gap between them is the only story here.

Every side is named from the FIXTURE's point of view, never a team's: \
`home` means the home team, `away` means the away team, `over` means the \
total went over. `won` and `lost` describe that side, not either team's \
fortunes. If a price is shown as `?` we did not record one -- say the \
price is unknown, or say nothing about it. Never state a price that is \
not printed below.

`signal` lines are opportunities our platform detected before kick-off -- an \
arbitrage above 1.0 is a guaranteed return across books, and a positive-EV \
price is one longer than the probability attached to it. If no `signal` line \
appears, the platform detected NOTHING on this fixture. Say so in those \
words or leave the subject alone entirely. Do not write that a signal was \
detected, that one paid off, or that one was missed. There was no signal.

Write two or three sentences, plain and specific:

1. The result, and whether the market had it roughly right or clearly wrong.
2. Which priced markets landed and which did not, using the numbers given.
   Never write `all`, `every`, `none of` or `both` about a GROUP of \
   markets -- name the ones you mean. A list of near-identical lines is \
   exactly where a false generalisation hides: on a 0-0 every `over` \
   line loses while `under 0.5` wins, and `all total goals markets \
   lost` is then a plain falsehood about a market printed right there.
3. Only where `signal` lines appear: whether the outcome each pointed at \
came in, and if it did not, say so plainly. \
A surebet that loses one leg is still a \
surebet; do not confuse a hedge with a prediction."""


class MarketOutcome(NamedTuple):
    """One market we priced before kick-off, and how it settled."""

    market: str
    pick: str
    best_odds: float | None
    verdict: str


class DetectedSignal(NamedTuple):
    """An opportunity the platform flagged before this match started."""

    kind: str            # "arbitrage" or "positive EV"
    market: str
    detail: str


class ResultBrief(NamedTuple):
    """Everything the model may know about one finished match."""

    event_id: str
    home_team: str
    away_team: str
    tournament: str | None
    kickoff: str
    score: str
    stats: list[str]
    markets: list[MarketOutcome]
    signals: list[DetectedSignal]


def _market_line(m: MarketOutcome) -> str:
    price = f"{m.best_odds:.2f}" if m.best_odds is not None else "?"
    return f"- market | {m.market} | {m.pick} | best pre-match price {price} | {m.verdict}"


def build_prompt(brief: ResultBrief) -> list[dict[str, str]]:
    """The messages for one finished fixture."""
    header = (
        f"{brief.home_team} v {brief.away_team}"
        f" | {brief.tournament or 'competition not recorded'}"
        f" | kicked off {brief.kickoff} (Europe/Berlin)"
        f"\nfinal | {brief.score}"
    )
    lines = [
        *(f"- stat | {s}" for s in brief.stats),
        *(_market_line(m) for m in brief.markets),
    ]
    if brief.signals:
        lines += [
            f"- signal | {s.kind} | {s.market} | {s.detail}" for s in brief.signals
        ]
    elif brief.markets:
        # Printed, not merely absent. The first version left the signal section
        # empty and trusted the model to notice nothing was there; it wrote "a
        # signal was detected, and the outcome it pointed at came in" about a
        # fixture that had none. An absence has to be stated to be read.
        lines.append("- signal | NONE - the platform detected no signal here")
    if not lines:
        # Said explicitly. A model handed two team names and a scoreline will
        # write a match report out of its own memory, which is the single
        # outcome this module exists to prevent.
        lines = ["(no settled markets or signals were recorded for this fixture)"]

    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": header + "\n\n" + "\n".join(lines)},
    ]


def brief_from_rows(
    fixture: Any,
    markets: list[dict[str, Any]],
    signals: list[dict[str, Any]],
) -> ResultBrief:
    """Assemble from warehouse rows, keyed by Snowflake's UPPER names."""
    stats = []
    if fixture.get("XG_HOME") is not None and fixture.get("XG_AWAY") is not None:
        stats.append(f"xG {fixture['XG_HOME']:.2f} - {fixture['XG_AWAY']:.2f}")
    if fixture.get("CORNERS_HOME") is not None:
        stats.append(
            f"corners {int(fixture['CORNERS_HOME'])} - {int(fixture['CORNERS_AWAY'] or 0)}"
        )

    return ResultBrief(
        event_id=str(fixture["EVENT_ID"]),
        home_team=str(fixture["HOME_TEAM"]),
        away_team=str(fixture["AWAY_TEAM"]),
        tournament=fixture.get("TOURNAMENT"),
        kickoff=f"{fixture['KICKOFF_AT']:%A %d %B %Y, %H:%M}",
        score=f"{int(fixture['GOALS_HOME'])}-{int(fixture['GOALS_AWAY'])}",
        stats=stats,
        markets=[
            MarketOutcome(
                market=str(r["MARKET"]),
                pick=str(r["PICK"]),
                best_odds=r.get("BEST_ODDS"),
                verdict=str(r["VERDICT"]),
            )
            for r in markets
        ],
        signals=[
            DetectedSignal(
                kind=str(r["KIND"]),
                market=str(r["MARKET"]),
                detail=str(r["DETAIL"]),
            )
            for r in signals
        ],
    )
