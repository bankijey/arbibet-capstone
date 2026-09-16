"""Write an LLM prose brief for each deep-dive fixture into
`gold_fixture_summary_ai`.

Every number the model sees comes from this warehouse: the price series in
`fact_odds_tick`, the match history in `fact_team_match`, and the settled
market rates in `fact_team_market_result`. Nothing else is supplied and the
system prompt forbids anything else being used -- a football fixture is exactly
the sort of subject a language model already has opinions about, and an
unverifiable sentence about a rivalry or a league position would be
indistinguishable from the parts that are measured.

Scope is every fixture that has NOT kicked off and carries a live signal --
an arbitrage above 1.0 or a positive EV. A brief is only worth writing about
a bet the reader can still place.

A brief is rewritten only when its evidence signature changes, and that
signature is quantised: see PRICE_BAND and ARB_STEP in `fixture_brief.py`
for the thresholds and the measurements behind them. This is what keeps the
half-hourly schedule from paying for a rewrite every time a price twitches.

Run:
    python enrich/fixture_summary.py
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import OpenAI

from arbibet_capstone.deep_dives import popular_fixtures_sql
from arbibet_capstone.env import load as load_env
from arbibet_capstone.env import require
from arbibet_capstone.fixture_brief import (
    MODEL,
    brief_from_rows,
    build_prompt,
    needs_rewrite,
)
from arbibet_capstone.warehouse import connect, merge

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("fixture_summary")

TABLE = "gold_fixture_summary_ai"
FORM_WINDOW = 10

# Fixtures that have NOT kicked off and carry a live signal: an arbitrage
# above 1.0, or a positive EV. That is the whole scope.
#
# It used to be "the five fixtures the most distinct booking slips were built
# on", which is a measure of what people BET on, not of what is worth telling
# them. A fixture nobody has slipped can still be the one with a surebet
# standing on it, and a heavily-slipped fixture that kicked off an hour ago
# cannot be acted on at all -- the brief was being written for the reader who
# had already missed it.
#
# `kickoff_at > current_timestamp()` is the load-bearing half. Every signal
# this platform found more than a few minutes after kick-off was an artefact
# of books suspending in-play at different moments (FINDINGS 12b), and a brief
# about one of those describes a bet that never existed.
_FIXTURES = """
    WITH signalled AS (
        SELECT event_id, max(arbitrage) AS best_arb, count(*) AS n
        FROM ANALYTICS.stg_arbitrage_leg
        WHERE is_surebet   -- above 1.0 AND fresh legs; FINDINGS 13g
        GROUP BY event_id
        UNION ALL
        SELECT event_id, NULL, count(*)
        FROM ANALYTICS.stg_ev_signal
        WHERE ev > 0 AND is_fresh
        GROUP BY event_id
    )
    SELECT f.event_id, f.home_team, f.away_team, f.tournament, f.kickoff_at,
           f.home_team_id, f.away_team_id
    FROM signalled s
    JOIN CORE.dim_fixture f ON f.event_id = s.event_id
    WHERE f.kickoff_at > current_timestamp()
    GROUP BY 1, 2, 3, 4, 5, 6, 7
    ORDER BY coalesce(max(s.best_arb), 0) DESC, f.kickoff_at ASC
    LIMIT %d
"""

# The upcoming deep-dive fixtures -- the most-slipped matches still to be
# played -- are briefed too, whether or not they carry a signal. Their pages
# are the ones readers open, and with signalled fixtures as the only scope
# every one of them opened with no summary: popular matches are exactly the
# efficiently priced ones, so they rarely carry an arbitrage or an EV.
_DEEP_DIVES = """
    SELECT f.event_id, f.home_team, f.away_team, f.tournament, f.kickoff_at,
           f.home_team_id, f.away_team_id
    FROM (%s) d
    JOIN CORE.dim_fixture f ON f.event_id = d.event_id
    WHERE d.upcoming
"""

# The signals standing on this fixture right now. Books are named by id, not
# by name: the prompt is the one place a real bookmaker's name would end up
# quoted verbatim in published prose, and the brief has no need of it. The
# SET still matters, so it is in the string the signature hashes -- a leg
# moving from one book to another is a different bet.
_SIGNALS = """
    SELECT 'arbitrage'                          AS kind,
           market_name || COALESCE(' ' || line, '') AS market,
           NULL                                 AS outcome,
           max(arbitrage)                       AS value,
           max(n_legs)                          AS legs,
           listagg(DISTINCT 'book ' || b.bookmaker_id, ', ')
             WITHIN GROUP (ORDER BY 'book ' || b.bookmaker_id) AS books
    FROM ANALYTICS.stg_arbitrage_leg l
    LEFT JOIN CORE.dim_bookmaker b ON b.bookmaker_name = l.bookmaker_name
    WHERE l.event_id = '%s' AND l.is_surebet
    GROUP BY 1, 2, 3
    UNION ALL
    SELECT 'positive EV',
           COALESCE(m.market_name, 'market ' || s.market_base_id),
           s.outcome_name,
           max(s.ev),
           1,
           listagg(DISTINCT 'book ' || b.bookmaker_id, ', ')
             WITHIN GROUP (ORDER BY 'book ' || b.bookmaker_id)
    FROM ANALYTICS.stg_ev_signal s
    LEFT JOIN CORE.dim_market m ON m.market_base_id = s.market_base_id
    LEFT JOIN CORE.dim_bookmaker b ON b.bookmaker_name = s.bookmaker_name
    WHERE s.event_id = '%s' AND s.ev > 0 AND s.is_fresh
    GROUP BY 1, 2, 3
    LIMIT 12
"""

# The price picture per market and outcome. `opening` and `latest` come from
# the first and last tick by fire_time -- MIN/MAX BY rather than MIN/MAX, which
# would give the cheapest and dearest price rather than the first and last.
_MARKETS = """
    SELECT
      market_name || COALESCE(' ' || line, '') AS market,
      outcome                                  AS outcome,
      count(DISTINCT bookmaker_name)           AS books,
      min_by(odds, fire_time)                  AS opening,
      max_by(odds, fire_time)                  AS latest,
      min(odds)                                AS lowest,
      max(odds)                                AS highest,
      count(*)                                 AS changes
    FROM ANALYTICS.stg_odds_tick
    WHERE event_id = '%s'
    GROUP BY 1, 2
    ORDER BY changes DESC
    LIMIT 12
"""

# Both sides' settled record. Only matches BEFORE kick-off: using a side's
# later results to describe how a fixture was priced is the one mistake that
# would make the whole brief worthless.
# Both sides' last ten matches, split BY COMPETITION: a league derby and an
# early cup round are different evidence, and one pooled record hid which was
# which from the model.
_FORM = """
    SELECT
      team_name                                             AS team,
      coalesce(league_name, 'competition not recorded')     AS competition,
      CASE WHEN team_id = %d THEN 'home' ELSE 'away' END    AS role,
      count(*)                                              AS matches,
      sum(CASE WHEN result = 'W' THEN 1 ELSE 0 END)         AS wins,
      sum(CASE WHEN result = 'D' THEN 1 ELSE 0 END)         AS draws,
      sum(CASE WHEN result = 'L' THEN 1 ELSE 0 END)         AS losses,
      avg(goals_for)                                        AS goals_for,
      avg(goals_against)                                    AS goals_against,
      avg(xg)                                               AS xg,
      avg(corners)                                          AS corners
    FROM (
        SELECT *, row_number() OVER (
                    PARTITION BY team_id ORDER BY match_date DESC
                  ) AS recency
        FROM CORE.fact_team_match
        WHERE team_id IN (%d, %d) AND match_date < '%s'
    )
    WHERE recency <= %d
    GROUP BY team_name, team_id, league_name
    ORDER BY team_id, matches DESC
"""

_SETTLED = """
    SELECT
      CASE WHEN team_id = %d THEN '%s' ELSE '%s' END     AS team,
      market_family, period, side_or_line,
      count(*)                                          AS matches,
      sum(CASE WHEN verdict = 'won' THEN 1 ELSE 0 END)  AS landed
    FROM (
        SELECT *, row_number() OVER (
                    PARTITION BY team_id, market_family, period, side_or_line
                    ORDER BY match_date DESC
                  ) AS recency
        FROM CORE.fact_team_market_result
        WHERE team_id IN (%d, %d) AND match_date < '%s'
    )
    WHERE recency <= %d
    GROUP BY 1, 2, 3, 4
    HAVING count(*) >= 5
    ORDER BY landed / matches DESC
    LIMIT 24
"""


def _rows(cur: Any, sql: str) -> list[dict[str, Any]]:
    cur.execute(sql)
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]


def main() -> int:
    limit = int(os.environ.get("FIXTURE_SUMMARY_LIMIT", "12"))
    client = OpenAI(api_key=require("OPENAI_KEY"))

    with connect() as warehouse:
        with warehouse.cursor() as cur:
            signalled = _rows(cur, _FIXTURES % limit)
            dives = _rows(cur, _DEEP_DIVES % popular_fixtures_sql())
            # Deep dives first: always briefed, never cut by the signal limit.
            seen: set[str] = set()
            fixtures = []
            for row in dives + signalled:
                if row["EVENT_ID"] not in seen:
                    seen.add(row["EVENT_ID"])
                    fixtures.append(row)
            log.info("scope: deep dives=%d signalled=%d", len(dives), len(signalled))
            # The newest brief per fixture, and the numbers it was written
            # against. One row per fixture, not the whole history: older rows
            # are the audit trail and have nothing to say about what to do now.
            cur.execute(
                f"""
                SELECT event_id, evidence
                FROM CORE.{TABLE}
                QUALIFY row_number() OVER (
                    PARTITION BY event_id ORDER BY generated_at DESC
                ) = 1
                """
            )
            previous = {
                event_id: json.loads(evidence) if evidence else None
                for event_id, evidence in cur.fetchall()
            }

        briefs = []
        for fixture in fixtures:
            home_id, away_id = fixture["HOME_TEAM_ID"], fixture["AWAY_TEAM_ID"]
            kickoff = f"{fixture['KICKOFF_AT']:%Y-%m-%d}"
            with warehouse.cursor() as cur:
                markets = _rows(cur, _MARKETS % fixture["EVENT_ID"])
                form: list[dict[str, Any]] = []
                settled: list[dict[str, Any]] = []
                # A third of fixtures never resolve to API-Football team ids.
                # That is a fact about the matcher, not a failure: the market
                # half of the brief still stands on its own.
                if home_id is not None and away_id is not None:
                    form = _rows(
                        cur,
                        _FORM % (home_id, home_id, away_id, kickoff, FORM_WINDOW),
                    )
                    settled = _rows(
                        cur,
                        _SETTLED
                        % (
                            home_id,
                            fixture["HOME_TEAM"],
                            fixture["AWAY_TEAM"],
                            home_id,
                            away_id,
                            kickoff,
                            FORM_WINDOW,
                        ),
                    )
                signals = _rows(cur, _SIGNALS % (fixture["EVENT_ID"], fixture["EVENT_ID"]))
            briefs.append(brief_from_rows(fixture, markets, form, settled, signals))

        pending = [b for b in briefs if needs_rewrite(previous.get(b.event_id), b.evidence())]
        log.info(
            "fixtures=%d unchanged within threshold=%d rewriting=%d",
            len(briefs),
            len(briefs) - len(pending),
            len(pending),
        )

        written: list[dict[str, object]] = []
        failed = 0
        for brief in pending:
            try:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=build_prompt(brief),  # type: ignore[arg-type]
                    max_tokens=420,
                    temperature=0.3,
                )
            except Exception:
                # One fixture's failure is not the run's. Logged with a
                # traceback so a rate limit is distinguishable from a bad
                # prompt.
                log.error("fixture %s failed", brief.event_id, exc_info=True)
                failed += 1
                continue

            text = (response.choices[0].message.content or "").strip()
            if not text:
                log.warning("fixture %s returned an empty brief", brief.event_id)
                failed += 1
                continue

            written.append(
                {
                    "event_id": brief.event_id,
                    "evidence_signature": brief.signature(),
                    "evidence": brief.evidence(),
                    "summary": text,
                    "model": MODEL,
                    "markets_described": len(brief.markets),
                    "sides_with_history": len(brief.form),
                }
            )

        if written:
            # `merge`, not `merge_bulk`: this writes at most a dozen rows and
            # one of them is a VARIANT. `merge_bulk` stages through pandas,
            # which has nowhere to put a nested value.
            merge(
                warehouse,
                table=TABLE,
                rows=written,
                key=["event_id", "evidence_signature"],
                json_columns={"evidence"},
            )

    log.info("briefed=%d failed=%d", len(written), failed)
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
