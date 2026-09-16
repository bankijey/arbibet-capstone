"""Write a once-only note on each finished, settled match.

    played fixture -> settled markets + detected signals -> gpt-4o-mini

Run:
    python enrich/result_summary.py

Scope: fixtures that have kicked off, have a score in `fact_team_match`, and do
not already have a row in `gold_fixture_result_ai` -- the past deep-dive fixtures
first. Runs every 30 minutes. A match whose API-Football stats have not arrived
yet has no score and is skipped, not written, so it is picked up by a later run:
one note before kick-off, one after full time, the second whenever the data
allows. A match is a settled fact,
so its note is written once and never revised -- re-running this skips
everything already done, which is what makes it safe on a daily schedule.

Deleting a row is how you ask for a rewrite. Nothing else triggers one.

Bounding knob:
    RESULT_SUMMARY_LIMIT   fixtures per run (default 10; this costs money)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from openai import OpenAI

from arbibet_capstone.crosswalk.settle.team_perspective import (
    TEAM_AWAY,
    TEAM_HOME,
    TeamPerspectiveRow,
    team_perspective,
)
from arbibet_capstone.deep_dives import popular_fixtures_sql
from arbibet_capstone.env import load as load_env
from arbibet_capstone.env import require
from arbibet_capstone.result_brief import MODEL, brief_from_rows, build_prompt
from arbibet_capstone.warehouse import connect, merge_bulk

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("result_summary")

TABLE = "gold_fixture_result_ai"

# Finished, scored, and not yet written up. Newest first: a note about last
# night's match is worth more than one about a match three weeks ago.
_FIXTURES = """
    SELECT f.event_id, f.home_team, f.away_team, f.tournament,
           f.kickoff_at::TIMESTAMP_LTZ AS kickoff_at,
           h.goals_for AS goals_home, h.goals_against AS goals_away,
           h.xg AS xg_home, a.xg AS xg_away,
           h.corners AS corners_home, a.corners AS corners_away
    FROM CORE.dim_fixture f
    JOIN CORE.fact_team_match h
      ON h.fixture_id = f.apifootball_id AND h.is_home = TRUE
    LEFT JOIN CORE.fact_team_match a
      ON a.fixture_id = f.apifootball_id AND a.is_home = FALSE
    LEFT JOIN (%s) d ON d.event_id = f.event_id AND NOT d.upcoming
    WHERE f.kickoff_at < current_timestamp()
      AND f.event_id NOT IN (SELECT event_id FROM CORE.%s)
    -- Deep-dive fixtures first: each has a page showing its pre-match brief,
    -- and the result note is the other half of it.
    ORDER BY (d.event_id IS NOT NULL) DESC, f.kickoff_at DESC
    LIMIT %d
"""

# How the markets this fixture was priced on actually settled, joined to the
# best price any book was showing for THAT market before kick-off.
#
# Two joins here are load-bearing and were both wrong in the first version:
#
# 1. The price join goes through `dim_market_outcome`, which is the bridge
#    between the settlement taxonomy (family, period, side) and the betradar
#    (market_id, outcome_id) that a tick is keyed on. Joining ticks on
#    `event_id` alone -- the obvious thing -- returns the longest price
#    anywhere in the fixture, identically, on every market line: a 25.0
#    outsider labelled as the price of a 1x2 home AND a BTTS yes.
#
# 2. `verdict` here is TEAM-RELATIVE (see team_perspective.py): on the home
#    team's row, side 'away' carries the mirrored verdict, so a 0-0 draw reads
#    `away -> won`. It is un-mirrored back to fixture level in Python, by the
#    module that declared the mirror. The raw columns come back for that.
_MARKETS = """
    SELECT
      r.market_family                    AS market_family,
      r.period                           AS period,
      r.side_or_line                     AS side_or_line,
      r.is_home                          AS is_home,
      r.verdict                          AS verdict,
      max(t.odds)                        AS best_odds
    FROM CORE.fact_team_market_result r
    JOIN CORE.dim_fixture f ON f.apifootball_id = r.fixture_id
    LEFT JOIN CORE.dim_market_outcome o
      ON o.market_family = r.market_family
     AND o.period        = r.period
     AND o.side          = split_part(r.side_or_line, '@', 1)
    LEFT JOIN ANALYTICS.stg_odds_tick t
      ON t.event_id   = f.event_id
     AND t.fire_time  < f.kickoff_at
     AND t.outcome_id = o.outcome_id
     AND split_part(t.market_id, ';', 1) = o.market_id
     AND COALESCE(t.line, '') = COALESCE(nullif(split_part(r.side_or_line, '@', 2), ''), '')
    WHERE f.event_id = '%s'
      AND r.verdict IN ('won', 'lost', 'push')
    GROUP BY 1, 2, 3, 4, 5
"""

# Anything the platform flagged before this match started.
_SIGNALS = """
    SELECT 'arbitrage' AS kind,
           market_name || COALESCE(' ' || line, '') AS market,
           'arbitrage ' || round(arbitrage, 4) || ' across ' || n_legs || ' legs' AS detail
    FROM ANALYTICS.stg_arbitrage_leg
    -- Surebets only. The table also holds near-misses below 1.0 and stale-leg
    -- sets, and the prompt tells the model an arbitrage is a guaranteed return.
    WHERE event_id = '%s' AND is_surebet
    GROUP BY 1, 2, 3
    UNION ALL
    SELECT 'positive EV',
           COALESCE(m.market_name, 'market ' || s.market_base_id),
           s.outcome_name || ' at ' || s.odds || ' (EV ' || round(s.ev, 3) || ')'
    FROM ANALYTICS.stg_ev_signal s
    LEFT JOIN CORE.dim_market m ON m.market_base_id = s.market_base_id
    WHERE s.event_id = '%s'
    LIMIT 12
"""


# Families a reader recognises, first. Everything else keeps its natural order
# behind them, so the LIMIT below cuts the obscure tail rather than the 1x2.
_FAMILY_RANK = {
    "1x2": 0,
    "double_chance": 1,
    "draw_no_bet": 2,
    "total_goals": 3,
    "btts": 4,
    "handicap": 5,
    "asian_handicap": 6,
    "correct_score": 7,
}
_MARKETS_SHOWN = 14
_PER_FAMILY = 3


def _fixture_level(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Un-mirror team-relative verdicts back to fixture level.

    `fact_team_market_result.verdict` is expressed from the row's team's point
    of view while `side_or_line` stays fixture-level, so on the home team's row
    side 'away' carries the MIRRORED verdict. Reading it as written is how a
    0-0 draw came out as "the away and draw markets won".

    The mirror is `won <-> lost` with push/void self-mirroring, which makes it
    an involution: applying `team_perspective` a second time with the SAME team
    returns the fixture-level verdict. So this asks the module that declared
    the mirror to undo it, rather than restating the rule here -- a second copy
    of that table is exactly what team_perspective.py's docstring forbids.

    A refusal (an unclassified family, an unknown side) drops the row. A market
    we cannot label with certainty is one the model must not see.
    """
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        which = TEAM_HOME if r["IS_HOME"] else TEAM_AWAY
        undone = team_perspective(
            str(r["VERDICT"]), str(r["MARKET_FAMILY"]), str(r["SIDE_OR_LINE"]), which
        )
        if not isinstance(undone, TeamPerspectiveRow):
            continue
        key = (str(r["MARKET_FAMILY"]), str(r["PERIOD"]), str(r["SIDE_OR_LINE"]))
        # Both teams' rows un-mirror to the same fixture-level verdict, so the
        # second one is a duplicate -- except that only one of them carries a
        # price when the other team's row had no matching tick.
        prior = out.get(key)
        if prior is not None and prior["BEST_ODDS"] is not None:
            continue
        out[key] = {
            "MARKET": f"{key[0]} {key[1]}",
            "PICK": key[2],
            "BEST_ODDS": r["BEST_ODDS"],
            "VERDICT": undone.result,
            "_RANK": _FAMILY_RANK.get(key[0], len(_FAMILY_RANK)),
        }
    ordered = sorted(out.values(), key=lambda r: (r["_RANK"], r["MARKET"], r["PICK"]))
    return _thin(ordered)[:_MARKETS_SHOWN]


def _thin(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """At most `_PER_FAMILY` lines per family, both verdicts kept.

    A goals ladder is six lines that differ only in a number, and handing all
    six to a model asked for three sentences guarantees it compresses them --
    it wrote "all total goals markets won" about a fixture where `under 0.5`
    was printed as lost, two lines above. Forbidding the word `all` in the
    prompt helped and did not fix it, because the pressure to generalise comes
    from the input, not the instruction.

    So: keep one line of each distinct verdict in a family FIRST, then fill by
    rank. The contrast inside a family is the part a reader wants, and a family
    whose lines are genuinely unanimous stays unanimous -- there is then no
    false generalisation available to make.
    """
    by_family: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_family.setdefault(r["MARKET"], []).append(r)

    kept: list[dict[str, Any]] = []
    for family_rows in by_family.values():
        seen: set[str] = set()
        picked = []
        for r in family_rows:              # one of each verdict, in rank order
            if r["VERDICT"] not in seen:
                seen.add(r["VERDICT"])
                picked.append(r)
        for r in family_rows:              # then fill the remaining slots
            if len(picked) >= _PER_FAMILY:
                break
            if r not in picked:
                picked.append(r)
        kept += [r for r in family_rows if r in picked]  # restore rank order
    return kept


def _rows(cur: Any, sql: str) -> list[dict[str, Any]]:
    cur.execute(sql)
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]


def main() -> int:
    limit = int(os.environ.get("RESULT_SUMMARY_LIMIT", "10"))
    client = OpenAI(api_key=require("OPENAI_KEY"))

    with connect() as warehouse:
        with warehouse.cursor() as cur:
            fixtures = _rows(cur, _FIXTURES % (popular_fixtures_sql(), TABLE, limit))
        log.info("finished fixtures awaiting a note: %d", len(fixtures))

        written: list[dict[str, object]] = []
        failed = 0
        for fixture in fixtures:
            event_id = str(fixture["EVENT_ID"])
            if fixture.get("GOALS_HOME") is None or fixture.get("GOALS_AWAY") is None:
                # Settled in dim_fixture's view but with no score recorded --
                # a match that was abandoned, or one flatten has not reached.
                log.info("%s has no score yet; skipping", event_id)
                continue
            with warehouse.cursor() as cur:
                markets = _fixture_level(_rows(cur, _MARKETS % event_id))
                signals = _rows(cur, _SIGNALS % (event_id, event_id))

            brief = brief_from_rows(fixture, markets, signals)
            try:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=build_prompt(brief),  # type: ignore[arg-type]
                    max_tokens=260,
                    temperature=0.3,
                )
            except Exception:
                log.error("fixture %s failed", event_id, exc_info=True)
                failed += 1
                continue

            text = (response.choices[0].message.content or "").strip()
            if not text:
                log.warning("fixture %s returned an empty note", event_id)
                failed += 1
                continue

            written.append(
                {
                    "event_id": event_id,
                    "summary": text,
                    "model": MODEL,
                    "markets_shown": len(brief.markets),
                    "signals_shown": len(brief.signals),
                }
            )

        if written:
            merge_bulk(warehouse, table=TABLE, rows=written, key=["event_id"])

    log.info("results written=%d failed=%d", len(written), failed)
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
