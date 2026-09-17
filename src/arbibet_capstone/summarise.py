"""Turning a booking slip's legs into a question an LLM can answer honestly.

The prompt is built here, apart from the API call, because the prompt is the
part with decisions in it. Two of those decisions are load-bearing and both
exist to stop the model saying something the data does not support.

**`historical_rate` is recent form, not a probability.** It is a base rate over
at most ten matches and it knows nothing about opponent strength: Osnabruck
have won 5 of 10 and are 38.89 to beat Bayern Munich, and "0.500 against their
usual opposition" is the honest reading. The prompt says so in those terms,
because a model handed a number next to a price will otherwise compare them as
if they were the same kind of thing.

**Absent history is a real answer.** A promoted side, a newly covered league,
or a market nobody has settled yet all produce no rows. The prompt is told to
say that rather than reach for a plausible-sounding record.
"""

from __future__ import annotations

import hashlib
from typing import Any, NamedTuple

MODEL = "gpt-4o-mini"

_SYSTEM = """You assess betting slips for a sports data platform. You are shown \
a slip other people have copied, leg by leg, with two numbers per leg.

BEGIN your answer with a single sentence in bold (wrapped in **double \
asterisks**) stating the slip's combined implied probability, which you are \
GIVEN. Use only the figures provided; do not compute or invent any number, and \
never treat the "copied by" count as a probability or a frequency.

If the slip is given a "1 in N" figure, it is genuinely unlikely: say so, and \
convey the scale using that same N -- roughly one such slip in N would land. \
Keep it a SCALE characterisation only: no lottery odds, no lightning strikes, \
no real-world statistics or numbers you were not given.

If instead the slip is described as a LIKELY combined outcome, it is not rare \
at all -- say plainly that the combined result is a likely one at the stated \
percentage. Do not force a rarity claim onto a likely slip.

Then, after the bold sentence, continue normally.

`implied` is what the bookmaker's price implies, before their margin is removed.

`form` is how often that exact market has landed for the relevant side in their \
last ten matches. It is RECENT FORM, not a probability for this fixture: it \
ignores who the opponent was, and ten matches is a small sample where one \
result moves it ten points. Never present it as the chance of the leg winning, \
and never say a leg is mispriced on form alone.

Each leg names the COMPETITION it is in. A form rate earned mostly in one \
competition is weaker evidence for a leg in another, and legs from cups, \
early rounds and friendlies are where recent form transfers least: say so \
where it matters. You do not know league positions or opponent strength: do \
not guess them.

Where a leg shows "form: none", there is no settled history for it. Say so if \
it matters. Do not invent a record.

After the bold opener, answer in two or three sentences, plain and specific. \
Name the legs that stand out and why.

Close with ONE short, level sentence that sets the slip's chance against the \
alternative a measured bettor has: a single price that beats its probability, \
or two prices that together return more than the stake whichever outcome \
lands. Keep it a comparison of odds and probability -- no products, no advice, \
no "you should", no exclamation. If the slip is a LIKELY one, say instead that \
its return is small for the risk it still carries."""


class SlipLeg(NamedTuple):
    fixture: str
    market: str
    pick: str
    odds: float
    implied_rate: float | None
    historical_rate: float | None
    wins: int | None
    matches: int | None
    # Defaulted so legs built without it (older callers, the tests) still build.
    competition: str | None = None


class Slip(NamedTuple):
    share_code: str
    followed_times: int | None
    legs: list[SlipLeg]
    # Computed by the warehouse (see enrich/summarise.py `_LEGS`), never in
    # Python and never by the model: the product of every leg's price, its
    # reciprocal as the whole slip's implied probability, and that as "1 in N".
    # Optional so a Slip can still be built in a test without them.
    combined_odds: float | None = None
    combined_probability: float | None = None
    one_in_n: int | None = None

    @property
    def legs_with_history(self) -> int:
        return sum(1 for leg in self.legs if leg.matches)

    def signature(self) -> str:
        """Identity of what this summary was written ABOUT.

        A slip loses legs as its matches kick off, so `share_code` alone names
        a bet that changes. Hashing the legs means a re-run over unchanged
        content costs nothing and a genuinely different slip earns a new
        summary.

        The form numbers are in the hash too, and that is not an optimisation.
        A summary describes the evidence as much as the legs, and the evidence
        moves independently of them: a fixture whose team ids resolve today may
        not resolve tomorrow, and settling more history changes a rate without
        touching a single price. Slip B2RY7A6 was caught on the dashboard
        saying "recent form of 90%" beside two legs reading "no history" --
        legs byte-identical, evidence gone, signature unchanged, so nothing
        ever regenerated it. Hashing the prices alone lets a summary outlive
        the numbers it is a summary of.
        """
        material = "|".join(
            f"{leg.fixture}:{leg.market}:{leg.pick}:{leg.odds}" f":{leg.wins}/{leg.matches}"
            for leg in self.legs
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def structure_signature(self) -> str:
        """Identity of the slip WITHOUT its prices.

        `signature()` includes every leg's odds, and a slip's payload carries
        the legs' LIVE odds: in 40 slips fetched 30 minutes apart, 39 had moved
        prices. Once slips were fetched half-hourly, that made nearly every
        summarised slip "changed" on every run, and the ten-per-run budget went
        on rewriting the same most-copied slips -- 28 of the first 30 upcoming
        verdicts were rewrites, while 248 upcoming slips never got one.

        This is what decides whether a verdict is rewritten: the legs
        themselves (fixture, market, pick) and the form evidence behind them.
        A leg lost at kick-off, a pick changed, a history that resolved or
        settled another match -- those rewrite. Price drift does not; the
        dashboard shows the slip's combined odds live beside the verdict.
        """
        material = "|".join(
            f"{leg.fixture}:{leg.market}:{leg.pick}:{leg.wins}/{leg.matches}" for leg in self.legs
        )
        return hashlib.sha256(material.encode()).hexdigest()


def _leg_line(leg: SlipLeg) -> str:
    implied = f"{leg.implied_rate:.0%}" if leg.implied_rate is not None else "?"
    if leg.matches:
        form = f"{leg.wins}/{leg.matches} ({leg.historical_rate:.0%})"
    else:
        form = "none"
    return (
        f"- {leg.fixture} ({leg.competition or 'competition not recorded'}) "
        f"| {leg.market}: {leg.pick} @ {leg.odds:.2f} "
        f"| implied: {implied} | form: {form}"
    )


def build_prompt(slip: Slip) -> list[dict[str, str]]:
    """The messages for one slip."""
    header = f"Slip {slip.share_code}"
    if slip.followed_times:
        header += f", copied by {slip.followed_times:,} people"
    if slip.combined_odds is not None:
        # Two decimals below 100, none above. Rounding 1.19 to "1" told the
        # model a two-leg slip at even money was "a long accumulator with a
        # remote chance" -- the opposite of the truth, and stated confidently.
        shown = (
            f"{slip.combined_odds:,.2f}"
            if slip.combined_odds < 100
            else f"{slip.combined_odds:,.0f}"
        )
        header += f". {len(slip.legs)} legs still bettable, combined odds {shown}."
    if slip.combined_probability is not None:
        # The figures for the bold opener, computed in the warehouse and given
        # finished so the model phrases rather than calculates. Below combined
        # odds of 2.0 the warehouse leaves `one_in_n` NULL -- "1 in 1" is
        # nonsense and drew a manufactured rarity claim onto an 84%-likely
        # slip -- so those are framed as a likely outcome instead.
        if slip.one_in_n:
            header += (
                f" Combined implied probability: about 1 in {slip.one_in_n:,} "
                f"({slip.combined_probability:.4%}); roughly one such slip in "
                f"{slip.one_in_n:,} lands."
            )
        else:
            header += (
                f" This is a LIKELY combined outcome: implied probability "
                f"{slip.combined_probability:.1%}."
            )

    return [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": header + "\n\n" + "\n".join(_leg_line(leg) for leg in slip.legs),
        },
    ]


def slips_from_rows(rows: list[dict[str, Any]]) -> list[Slip]:
    """Group `gold_slip_leg_history` rows into slips."""
    grouped: dict[str, Slip] = {}
    for row in rows:
        code = str(row["SHARE_CODE"])
        leg = SlipLeg(
            fixture=f"{row['HOME_TEAM']} v {row['AWAY_TEAM']}",
            market=str(row["MARKET_NAME"] or row["MARKET_FAMILY"]),
            pick=str(row["OUTCOME_NAME"] or row["SIDE_OR_LINE"]),
            odds=float(row["ODDS"]),
            implied_rate=row["IMPLIED_RATE"],
            historical_rate=row["HISTORICAL_RATE"],
            wins=row["WINS"],
            matches=row["MATCHES"],
            competition=row.get("TOURNAMENT"),
        )
        if code not in grouped:
            grouped[code] = Slip(
                code,
                row["FOLLOWED_TIMES"],
                [],
                # Same value on every leg row of a slip; take it from whichever
                # arrives first. `.get` so a caller passing only leg columns
                # (the tests) still builds a Slip.
                combined_odds=row.get("COMBINED_ODDS"),
                combined_probability=row.get("COMBINED_PROBABILITY"),
                one_in_n=int(row["ONE_IN_N"]) if row.get("ONE_IN_N") else None,
            )
        grouped[code].legs.append(leg)
    return list(grouped.values())
