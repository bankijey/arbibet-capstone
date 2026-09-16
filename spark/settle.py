"""Settle historical markets per team into `fact_team_market_result`.

Silver's D15 grain: one row per team per settled market. Its payoff is that
every settled market becomes a form dimension for free -- "over 2.5 in 7 of the
last 10", "BTTS rate at home", head-to-head -- with no schema change, because
the question is a filter on this one table.

The engine is vendored whole from arbibet-silver and not reimplemented:
`settle()` reads the score basis the market declares, and `team_perspective()`
re-expresses a fixture-level verdict from one team's point of view, mirroring
it for directional families and leaving it alone for symmetric ones. Both are
pure, which is why this file is only plumbing.

Not a Spark job despite living in `spark/`: the input is `fact_team_match` in
Snowflake, already flattened, and the work is a nested loop over rows in
Python. Naming it honestly matters more than tidiness of folder.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Any

from arbibet_capstone.crosswalk.settle import betradar_market_map as market_map
from arbibet_capstone.crosswalk.settle import betradar_outcome_map as outcome_map
from arbibet_capstone.crosswalk.settle.settlement import (
    PeriodResolvedScores,
    TeamScore,
    settle,
)
from arbibet_capstone.crosswalk.settle.team_perspective import (
    TeamPerspectiveRow,
    team_perspective,
)
from arbibet_capstone.dims import ARB_RELEVANT_MARKETS
from arbibet_capstone.env import load as load_env
from arbibet_capstone.warehouse import connect, merge_bulk

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("settle")

# Lines are the one thing the maps cannot enumerate: a family declares that it
# HAS a line key, never which lines a book will offer. This ladder covers the
# goal totals that carry real liquidity; widening it is adding numbers here.
GOAL_LINES = (0.5, 1.5, 2.5, 3.5, 4.5)

# Which families to settle, and over which periods.
#
# The full catalogue is 222 combinations, and settling it across every fixture
# is 66 million rows describing markets nobody asks about. These five are the
# form dimensions a summary actually uses -- "over 2.5 in 7 of the last 10",
# "BTTS rate", "wins" -- and restricting to the match period drops the 1h/2h
# variants that triple the count for a question no one has asked yet.
#
# Widening is changing these two tuples. The engine already handles the rest.
FORM_FAMILIES = tuple(
    os.environ.get(
        "SETTLE_FAMILIES", "1x2,double_chance,btts,total_goals,draw_no_bet"
    ).split(",")
)
FORM_PERIODS = tuple(os.environ.get("SETTLE_PERIODS", "match").split(","))

# Silver's D15 form window: the last N matches per team. A serving parameter
# read at QUERY time, never a loading boundary -- which is why this table holds
# every settled match and the "last 10" lives in the form query.
FORM_WINDOW = int(os.environ.get("FORM_WINDOW", "10"))

# `handicap` does not take a decimal line. It uses betradar's goal-handicap
# notation -- a start-of-match scoreline -- and feeding it "2.5" earned a
# `line_invalid` refusal on all 6,000 of its rows rather than a wrong verdict.
# Asian handicap does take decimals, and settles fine on GOAL_LINES.
LINES_BY_FAMILY: dict[str, tuple[str, ...]] = {
    "handicap": ("0:1", "1:0", "0:2", "2:0"),
}


def _lines(family: str) -> tuple[str, ...]:
    explicit = LINES_BY_FAMILY.get(family)
    return explicit if explicit else tuple(f"{line:g}" for line in GOAL_LINES)

# API-Football's status codes to the engine's own vocabulary. An explicit map
# rather than `.lower()`: the two agreeing in case is a coincidence for these
# three and not a rule -- "ABD" is `abandoned`, "NS" is `not_started`. The
# engine refuses an unrecognised token with `unknown_status` rather than
# guessing, which is how this mapping's absence was caught: 87,600 rows of
# `unsettleable` instead of a plausible verdict.
STATUS_TO_ENGINE = {"FT": "ft", "AET": "aet", "PEN": "pen"}

# `side_or_line` joins the two with "@" -- silver's 0005d format, and the
# format `parse_side_or_line` expects on the way back in.
LINE_SEPARATOR = outcome_map.LINE_SEPARATOR


def catalogue() -> list[tuple[str, str, str, str, str]]:
    """`(market_id, family, period, time_basis, side_or_line)` to settle.

    Derived from the vendored maps rather than hand-listed: `market_map` gives
    the family, period and the rule that fixes the time basis, and
    `outcome_map` gives that family's sides and whether it carries a line. A
    hand-written table would be the same knowledge in a second place, drifting.

    Restricted to `ARB_RELEVANT_MARKETS` -- the markets these five books
    actually price. Settling all 148 mapped ids across every line would produce
    tens of millions of rows describing markets no one in this pipeline quotes.
    """
    entries: list[tuple[str, str, str, str, str]] = []
    for market_id in sorted(str(m) for m in ARB_RELEVANT_MARKETS):
        mapping = market_map.lookup(market_id)
        if mapping is None:
            continue
        family = outcome_map.lookup(mapping.market_family)
        if family is None:
            # The honest answer is None and the caller must treat it as one:
            # counted below, never a guessed side.
            continue
        basis = market_map.TIME_BASIS_BY_RULE.get(mapping.rule)
        if basis is None:
            continue
        if mapping.market_family not in FORM_FAMILIES or mapping.period not in FORM_PERIODS:
            continue
        for side in sorted(set(family.sides.values())):
            if family.line_keys:
                entries.extend(
                    (market_id, mapping.market_family, mapping.period, basis,
                     f"{side}{LINE_SEPARATOR}{line}")
                    for line in _lines(mapping.market_family)
                )
            else:
                entries.append(
                    (market_id, mapping.market_family, mapping.period, basis, side)
                )
    return entries


def scores(row: dict[str, Any]) -> PeriodResolvedScores:
    """D18's period set for one fixture, from the home team's row.

    `reg` is the 90-minute score and the default basis; `full` adds extra time;
    penalties enter neither (D30). A component the source did not publish stays
    None, and the engine refuses with `missing_score_component` rather than
    substituting a zero.
    """

    def pair(for_key: str, against_key: str) -> TeamScore | None:
        home, away = row.get(for_key), row.get(against_key)
        if home is None or away is None:
            return None
        return TeamScore(home=int(home), away=int(away))

    return PeriodResolvedScores(
        h1=pair("GOALS_FOR_H1", "GOALS_AGAINST_H1"),
        h2=pair("GOALS_FOR_H2", "GOALS_AGAINST_H2"),
        reg=pair("GOALS_FOR_REG", "GOALS_AGAINST_REG"),
        et=pair("GOALS_FOR_ET", "GOALS_AGAINST_ET"),
        pens=pair("GOALS_FOR_PENS", "GOALS_AGAINST_PENS"),
        full=pair("GOALS_FOR_FULL", "GOALS_AGAINST_FULL"),
    )


def fixture_rows(
    row: dict[str, Any], entries: list[tuple[str, str, str, str, str]]
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Every settled market for one fixture, from both teams' perspectives."""
    period_scores = scores(row)
    status = STATUS_TO_ENGINE.get(str(row["STATUS"]), str(row["STATUS"]))
    home_id, away_id = int(row["TEAM_ID"]), int(row["OPPONENT_ID"])
    refusals: Counter[str] = Counter()
    out: list[dict[str, Any]] = []

    for _market_id, family, period, basis, side_or_line in entries:
        verdict = settle(period_scores, status, family, period, basis, side_or_line)

        for which, team_id, opponent_id, is_home in (
            ("home", home_id, away_id, True),
            ("away", away_id, home_id, False),
        ):
            expressed = team_perspective(verdict.verdict, family, side_or_line, which)
            if not isinstance(expressed, TeamPerspectiveRow):
                # A family the classification table does not cover is REFUSED,
                # never defaulted to symmetric. Counted so the refusals stay
                # visible as the map's to-do list.
                refusals[expressed.reason] += 1
                continue
            out.append(
                {
                    "fixture_id": int(row["FIXTURE_ID"]),
                    "team_id": team_id,
                    "opponent_id": opponent_id,
                    "match_date": row["MATCH_DATE"],
                    "is_home": is_home,
                    "market_family": family,
                    "period": period,
                    "side_or_line": side_or_line,
                    "verdict": expressed.result,
                    "reason": verdict.reason,
                }
            )
    return out, refusals


def main() -> None:
    since_days = int(os.environ.get("SETTLE_SINCE_DAYS", "365"))
    # Same reasoning as FLATTEN_LIMIT: 225 combinations times two teams times
    # every fixture is over a million rows, and proving the verdicts on a few
    # hundred is cheaper than discovering a mirrored side after all of them.
    limit = os.environ.get("SETTLE_LIMIT")
    entries = catalogue()
    log.info("catalogue: %d (market, side) combinations", len(entries))

    with connect() as warehouse:
        with warehouse.cursor() as cur:
            # One row per fixture, not per team: the score set is
            # fixture-level, and team_perspective produces both sides from it.
            # Scope is the WHOLE history, not the teams currently priced.
            #
            # A relevance bound was written first -- teams in dim_fixture,
            # their last FORM_WINDOW matches -- and then deleted. Settling
            # everything took 80 seconds to compute and 80 to write for 5.95M
            # rows, and the result serves any FUTURE fixture, where a bound
            # would need recomputing every time dim_fixture moves. Cheap and
            # permanent beats clever and stale.
            cur.execute(
                "SELECT fixture_id, team_id, opponent_id, match_date, status, "
                "goals_for_h1, goals_against_h1, goals_for_h2, goals_against_h2, "
                "goals_for_reg, goals_against_reg, goals_for_et, goals_against_et, "
                "goals_for_pens, goals_against_pens, goals_for_full, goals_against_full "
                "FROM fact_team_match WHERE is_home = TRUE "
                f"AND match_date >= DATEADD(day, -{since_days}, CURRENT_TIMESTAMP())"
                + (f" LIMIT {limit}" if limit else "")
            )
            columns = [c[0] for c in cur.description]
            fixtures = [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]
        log.info("fixtures to settle: %d", len(fixtures))

        rows: list[dict[str, Any]] = []
        refusals: Counter[str] = Counter()
        for fixture in fixtures:
            fixture_out, fixture_refusals = fixture_rows(fixture, entries)
            rows.extend(fixture_out)
            refusals.update(fixture_refusals)

        verdicts = Counter(r["verdict"] for r in rows)
        log.info("rows=%d verdicts=%s", len(rows), dict(verdicts.most_common()))
        if refusals:
            log.info("team-perspective refusals=%s", dict(refusals.most_common()))

        written = merge_bulk(
            warehouse,
            table="fact_team_market_result",
            rows=rows,
            key=["fixture_id", "team_id", "market_family", "period", "side_or_line"],
        )
    log.info("fact_team_market_result=%d", written)


if __name__ == "__main__":
    main()
