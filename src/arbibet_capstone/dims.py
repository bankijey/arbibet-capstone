"""Warehouse dimension rows.

Small, slow-changing reference data: the market crosswalk and the fixture list.
Both are built here as plain rows so they can be tested without a warehouse,
and loaded by `snowflake/load_dims.py`.

Deliberately not a Spark job. `dim_market` is 236 CSV rows and `dim_fixture` a
few hundred; running either through Spark would mean version-matching the
Snowflake connector jars for data that fits in memory a thousand times over.
Spark earns its place on the 154,088-payload flatten job, not here.
"""

from __future__ import annotations

from typing import Any

from arbibet_capstone.crosswalk.mappings import market_mappings
from arbibet_capstone.fixtures import Fixture

# Betradar market ids the vendored parsers actually translate, taken from the
# branches of `_map_bet9ja_outcome` and `_map_lsb_outcome`. These are the
# markets that can carry a cross-book comparison at all, because they are the
# only ones both dialect books resolve into canonical outcome ids.
#
# A stated judgment, not a fact from the CSV -- which is why it is one frozen
# set with a comment rather than a rule rediscovered in three places.
ARB_RELEVANT_MARKETS = frozenset(
    {
        1, 60, 83,                              # 1X2, by period
        10, 63, 85,                             # double chance, by period
        11, 64, 86,                             # draw-no-bet, by period
        13,                                     # away-no-bet
        14,                                     # handicap
        16,                                     # asian handicap
        18, 68, 90, 177,                        # over/under, by period + corners
        29, 31, 32, 34, 49, 51, 57, 75, 95,     # yes/no families (BTTS, clean sheet, ...)
        35, 78, 543,                            # 1X2 & BTTS combinations
        37, 79, 544,                            # 1X2 & over/under combinations
        542, 545, 546,                          # double chance & BTTS combinations
    }
)


def market_rows() -> list[dict[str, Any]]:
    """`dim_market` rows from the crosswalk CSV.

    Deduplicated on `market_base_id`: the CSV carries id 142 twice, once per
    bet9ja key (`S_BTLC` and `S_EXACTBOOK`), because one betradar market can
    have more than one name at a book. The market is still one market.
    """
    frame = market_mappings()
    rows: dict[int, dict[str, Any]] = {}
    for record in frame.to_dict("records"):
        base = int(record["marketId"])
        if base in rows:
            continue
        livescorebet = record.get("livescorebet")
        bet9ja = record.get("bet9ja")
        rows[base] = {
            "market_base_id": base,
            "market_name": str(record.get("marketName") or f"market {base}"),
            "bet9ja_key": None if _missing(bet9ja) else str(bet9ja),
            "livescorebet_type": None if _missing(livescorebet) else int(livescorebet),
            "is_arb_relevant": base in ARB_RELEVANT_MARKETS,
        }
    return list(rows.values())


def fixture_rows(fixtures: list[Fixture]) -> list[dict[str, Any]]:
    """`dim_fixture` rows, carrying both identity spaces."""
    return [
        {
            "event_id": str(f.event_id),
            "apifootball_id": f.apifootball_id,
            "kickoff_at": f.kickoff,
            "tournament": f.tournament,
            "home_team": f.home_team,
            "away_team": f.away_team,
            "home_team_id": f.home_team_id,
            "away_team_id": f.away_team_id,
            "sr_match_id": f.sr_match_id,
        }
        for f in fixtures
    ]


def _missing(value: Any) -> bool:
    """True for None and for pandas' NA/NaN, which are not None and not falsy."""
    return value is None or value != value


def market_outcome_rows() -> list[dict[str, Any]]:
    """`dim_market_outcome`: the betradar maps, materialised.

    The settlement engine's two maps are Python data, which means only Python
    can use them. Landing them as a dimension lets dbt join a booking slip's
    `(market.id, outcome.id)` straight onto `(market_family, period, side)` in
    SQL -- and the LLM summariser, the dashboard and anything later read the
    same table rather than each re-deriving it.

    Still one definition: this reads the maps rather than restating them.
    """
    from arbibet_capstone.crosswalk.settle import betradar_market_map as market_map
    from arbibet_capstone.crosswalk.settle import betradar_outcome_map as outcome_map
    from arbibet_capstone.crosswalk.settle.team_perspective import (
        FAMILY_CLASSIFICATION,
        TeamPerspectiveRow,
        team_perspective,
    )

    def primary_team(family: str, side: str) -> str | None:
        """Which team's row carries this side's FIXTURE-level verdict.

        `fact_team_market_result` mirrors the verdict on the non-primary team's
        row (FINDINGS 13c: read naively, a 0-0 draw said "away won"). Asked
        rather than restated: the team whose perspective leaves a `won` intact
        is primary; both, for symmetric families and sides like `draw`. SQL
        then picks that row and never has to mirror anything.
        """
        kept = {
            team
            for team in ("home", "away")
            if isinstance(r := team_perspective("won", family, side, team), TeamPerspectiveRow)
            and r.result == "won"
        }
        if kept == {"home", "away"}:
            return "both"
        return kept.pop() if kept else None

    rows: list[dict[str, Any]] = []
    for market_id, mapping in market_map.BETRADAR_MARKET_MAP.items():
        family = outcome_map.lookup(mapping.market_family)
        if family is None:
            continue
        basis = market_map.TIME_BASIS_BY_RULE.get(mapping.rule)
        for outcome_id, side in family.sides.items():
            rows.append(
                {
                    "market_id": str(market_id),
                    "outcome_id": str(outcome_id),
                    "market_family": mapping.market_family,
                    "period": mapping.period,
                    "time_basis": basis,
                    "side": side,
                    "has_line": bool(family.line_keys),
                    # symmetric | directional | team_scoped_home |
                    # team_scoped_away | not_team_relevant. Carried because
                    # pooling two teams' histories is only valid for a
                    # symmetric family, where both observe the same fact.
                    "classification": FAMILY_CLASSIFICATION.get(mapping.market_family),
                    "primary_team": primary_team(mapping.market_family, side),
                    "market_name": mapping.name,
                }
            )
    return rows
