"""Publish the dashboard's data as static snapshots for the web app.

The web app (`web/`, Next.js on Vercel) never queries Snowflake. This job does
it once per pipeline run, straight after the enrichment steps, while the
warehouse is already awake for the run -- so a visitor costs no warehouse time
at all, however many there are. The Streamlit dashboard queried on demand and
kept a background thread polling; the September 2026 cost review found that
design keeping COMPUTE_WH awake around the clock.

Two kinds of file, both gzipped JSON:

    live.json.gz            everything that moves: headline numbers, arbitrage
                            and EV signals with their charts, the backtest,
                            popular slips, and deep dives for fixtures still
                            to be played or kicked off since the last archive.
    archive/<day>.json.gz   deep dives for one Berlin calendar day of played,
                            slipped fixtures. Written once the day's last match
                            is three hours old and rewritten only by the daily
                            run (`--archive-recent`), so post-match notes that
                            arrive later still land.

Uploads are sparing on purpose. Vercel Blob's Hobby tier includes 2,000 write
operations a month and blocks the store for 30 days past it, so every file is
written only when its content hash changed, and there is one live file rather
than one per section. An hourly run is at most ~720 writes a month.

Local runs always write to `web/.data/`; the web app reads that directory in
development. Uploads need BLOB_READ_WRITE_TOKEN. After an upload the web app is
told to revalidate (WEB_REVALIDATE_URL + WEB_REVALIDATE_SECRET), so pages
update within seconds of a run rather than on a timer.

Run:
    python publish/snapshot.py                   # live + any missing archive day
    python publish/snapshot.py --archive-recent  # also rewrite the last 3 days
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import math
import os
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
# The chart reduction and the backtest are the dashboard's own, pure pandas and
# tested; importing them keeps one definition of each.
sys.path.insert(0, str(ROOT))

from dashboard.backtest import (  # noqa: E402
    SIZINGS,
    SUREBET_MIN,
    TIMINGS,
    choose,
    compare,
    paper_wallet,
    simulate,
)
from dashboard.series import POINTS, reduce_steps  # noqa: E402

from arbibet_capstone.env import load as load_env  # noqa: E402
from arbibet_capstone.warehouse import connect  # noqa: E402

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("publish.snapshot")

OUT = ROOT / "web" / ".data"
TIMEZONE = "Europe/Berlin"

UPCOMING_DIVES = 15  # ten are shown; the rest cover fixtures kicking off before the next run
RECENT_DIVES = 30  # played since the newest archived day, most-slipped first
SLIP_CARDS = 25  # per side, re-split by the clock in the browser
PICKS = 150  # biggest winning and losing picks each, for the scatter
DIVE_MARKETS = 10  # busiest markets charted per deep dive
FORM_WINDOW = 10
SETTLE_DELAY = timedelta(hours=3)


# --- serialisation ---------------------------------------------------------------


def _iso(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NaT:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(TIMEZONE)
    return stamp.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value: Any) -> Any:
    """JSON-safe: NaN to null, numpy scalars to Python, timestamps to ISO UTC."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, pd.Timestamp | datetime):
        return _iso(value)
    if hasattr(value, "item") and not isinstance(value, str | bytes):
        value = value.item()
    if isinstance(value, Decimal):
        # Snowflake NUMBER columns arrive as Decimal.
        value = int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else round(value, 6)
    if value is pd.NA or value is pd.NaT:
        return None
    return value


def _records(frame: pd.DataFrame, columns: dict[str, str]) -> list[dict[str, Any]]:
    """Rows as dicts, renamed from warehouse UPPERCASE to the web app's keys."""
    if frame.empty:
        return []
    picked = frame[list(columns)].rename(columns=columns)
    return [_clean(row) for row in picked.to_dict(orient="records")]


def _points(frame: pd.DataFrame, x: str, *ys: str) -> list[list[Any]]:
    return [
        [_iso(row[0]), *(_clean(v) for v in row[1:])]
        for row in frame[[x, *ys]].itertuples(index=False)
    ]


# --- warehouse ---------------------------------------------------------------------


class Warehouse:
    def __init__(self) -> None:
        self.conn = connect()

    def query(self, sql: str) -> pd.DataFrame:
        with self.conn.cursor() as cur:
            cur.execute(sql)
            return pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])

    def close(self) -> None:
        self.conn.close()


def _quoted(values: Iterable[Any]) -> str:
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


# --- live: market signals ------------------------------------------------------------

JOIN = ["EVENT_ID", "MARKET_ID", "OUTCOME_ID", "BOOKMAKER_NAME"]


def headline(wh: Warehouse) -> dict[str, Any]:
    asof = wh.query(
        """
        SELECT
          (SELECT max(detected_at)  FROM CORE.fact_arbitrage_signal) AS last_signal,
          (SELECT max(generated_at) FROM CORE.gold_slip_summary_ai)  AS last_summary,
          (SELECT max(fire_time)    FROM CORE.fact_odds_tick)        AS last_tick
        """
    ).iloc[0]
    # Only TRUE surebets: fresh legs, priced before kick-off (FINDINGS 13g).
    counts = wh.query(
        """
        SELECT
          (SELECT count(*) FROM ANALYTICS.stg_arbitrage_signal a
            JOIN CORE.dim_fixture f USING (event_id)
            WHERE a.is_surebet AND a.detected_at < f.kickoff_at)                   AS surebets,
          (SELECT count(*) FROM ANALYTICS.stg_ev_signal s
            JOIN CORE.dim_fixture f USING (event_id)
            WHERE s.detected_at < f.kickoff_at)                                    AS ev_signals,
          (SELECT count(*) FROM CORE.fact_odds_tick)                               AS ticks,
          (SELECT count(*) FROM CORE.fact_team_market_result)                      AS settled,
          (SELECT count(DISTINCT share_code) FROM ANALYTICS.gold_slip_leg_history) AS slips
        """
    ).iloc[0]
    example = wh.query(
        """
        SELECT fixture, market_name, line, outcome, bookmaker_name, odds, spread_seconds,
               detected_at
        FROM ANALYTICS.stg_arbitrage_leg
        WHERE signal_key = (
            SELECT signal_key FROM ANALYTICS.stg_arbitrage_signal
            WHERE is_surebet ORDER BY detected_at DESC LIMIT 1
        )
        """
    )
    freshness = wh.query(
        """
        SELECT
          CASE
            WHEN leg_spread_seconds <= 60    THEN 'under 1 min'
            WHEN leg_spread_seconds <= 300   THEN '1-5 min'
            WHEN leg_spread_seconds <= 3600  THEN '5-60 min'
            ELSE 'over an hour'
          END                                         AS spread,
          count(*)                                    AS signals,
          sum(CASE WHEN is_surebet THEN 1 ELSE 0 END) AS surebets
        FROM ANALYTICS.stg_arbitrage_signal
        GROUP BY 1
        ORDER BY min(leg_spread_seconds)
        """
    )
    worked = None
    if not example.empty:
        head = example.iloc[0]
        worked = {
            "fixture": head.FIXTURE,
            "market": head.MARKET_NAME,
            "line": head.LINE,
            "detectedAt": _iso(head.DETECTED_AT),
            "spreadSeconds": _clean(head.SPREAD_SECONDS),
            "legs": _records(
                example, {"OUTCOME": "outcome", "BOOKMAKER_NAME": "book", "ODDS": "odds"}
            ),
        }
    return {
        "asof": {
            "lastSignal": _iso(asof.LAST_SIGNAL),
            "lastTick": _iso(asof.LAST_TICK),
            "lastSummary": _iso(asof.LAST_SUMMARY),
        },
        "counts": {
            "surebets": int(counts.SUREBETS),
            "evSignals": int(counts.EV_SIGNALS),
            "ticks": int(counts.TICKS),
            "settled": int(counts.SETTLED),
            "slips": int(counts.SLIPS),
        },
        "example": worked,
        "freshness": _records(
            freshness, {"SPREAD": "spread", "SIGNALS": "signals", "SUREBETS": "surebets"}
        ),
    }


def signals(wh: Warehouse) -> dict[str, Any]:
    legs = wh.query(
        """
        SELECT l.signal_key, l.event_id, l.market_id, l.outcome_id,
               l.fixture, l.market_name, l.line, l.outcome,
               l.bookmaker_name, l.odds, u.url, l.arbitrage, l.spread_seconds,
               l.detected_at, l.kickoff_at, l.detected_at < l.kickoff_at AS pre_match
        FROM ANALYTICS.stg_arbitrage_leg l
        LEFT JOIN ANALYTICS.stg_event_link u
          ON u.event_id = l.event_id AND u.bookmaker_name = l.bookmaker_name
        WHERE l.is_surebet
        ORDER BY l.arbitrage DESC, l.fixture, l.outcome
        """
    )
    track = wh.query(
        """
        SELECT event_id, market_id, observed_at, arbitrage, leg_spread_seconds
        FROM ANALYTICS.stg_arbitrage_track
        ORDER BY observed_at
        """
    )
    ev_all = wh.query(
        """
        SELECT
          s.event_id, s.market_id, s.outcome_id,
          COALESCE(f.home_team || ' v ' || f.away_team, s.event_id) AS fixture,
          COALESCE(m.market_name, 'market ' || s.market_base_id)    AS market,
          s.specifier AS line, s.outcome_name, s.bookmaker_name,
          s.odds, u.url, s.implied_p, s.p_source, pu.url AS p_source_url,
          s.probability_spread_seconds, s.ev,
          f.kickoff_at, s.detected_at, s.is_fresh
        FROM ANALYTICS.stg_ev_signal s
        LEFT JOIN CORE.dim_fixture f ON f.event_id = s.event_id
        LEFT JOIN ANALYTICS.stg_event_link u
          ON u.event_id = s.event_id AND u.bookmaker_name = s.bookmaker_name
        -- The fixture at the book the probability was borrowed from.
        LEFT JOIN ANALYTICS.stg_event_link pu
          ON pu.event_id = s.event_id AND pu.bookmaker_name = s.p_source
        LEFT JOIN CORE.dim_market m ON m.market_base_id = s.market_base_id
        WHERE s.detected_at < f.kickoff_at
        ORDER BY s.ev DESC
        """
    )
    ticks = wh.query(
        """
        SELECT t.event_id, t.market_id, t.outcome_id, t.bookmaker_name, t.odds, t.fire_time
        FROM ANALYTICS.stg_odds_tick t
        JOIN (
            SELECT DISTINCT event_id, market_id FROM ANALYTICS.stg_arbitrage_leg WHERE is_surebet
            UNION
            SELECT DISTINCT event_id, market_id FROM ANALYTICS.stg_ev_signal
        ) m ON m.event_id = t.event_id AND m.market_id = t.market_id
        """
    )
    availability = wh.query(
        """
        SELECT a.event_id, a.market_id, a.outcome_id, b.bookmaker_name,
               a.offered, a.current_odds, a.book_fired_at
        FROM CORE.fact_leg_availability a
        JOIN CORE.dim_bookmaker b USING (bookmaker_id)
        """
    )
    flags = wh.query(
        """
        SELECT event_id, market_id, bookmaker_name, flagged_at
        FROM CORE.dashboard_leg_flag
        WHERE active
        """
    )
    stale = wh.query(
        """
        SELECT count(*) AS n, max(arbitrage) AS worst
        FROM ANALYTICS.stg_arbitrage_signal
        WHERE arbitrage > 1 AND NOT is_fresh
        """
    ).iloc[0]
    efficiency = {
        name: _records(
            wh.query(
                f"""
                SELECT COALESCE(m.market_name, 'market ' || s.market_base_id)
                         || COALESCE(' ' || {grain}, '')        AS label,
                       count(*)                                  AS signals,
                       avg(s.overround)                          AS mean_overround
                FROM ANALYTICS.stg_arbitrage_signal s
                LEFT JOIN CORE.dim_market m ON m.market_base_id = s.market_base_id
                GROUP BY 1
                ORDER BY 3
                """
            ),
            {"LABEL": "label", "SIGNALS": "signals", "MEAN_OVERROUND": "meanOverround"},
        )
        for name, grain in (("byMarket", "NULL"), ("byLine", "s.specifier"))
    }
    by_book = wh.query(
        """
        SELECT bookmaker_name, count(*) AS signals, avg(ev) AS mean_ev
        FROM ANALYTICS.stg_ev_signal
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    settled = wh.query(
        """
        SELECT signal_key, event_id, market_id, outcome_id, bookmaker_name, odds, ev,
               detected_at, kickoff_at, verdict
        FROM ANALYTICS.gold_ev_settled
        """
    )

    for frame in (legs, ev_all, ticks, availability, settled):
        frame["OUTCOME_ID"] = frame.OUTCOME_ID.astype(str)

    # Latest price per (market, outcome, book), as of kick-off for a played match.
    kickoffs = pd.concat(
        [legs[["EVENT_ID", "KICKOFF_AT"]], ev_all[["EVENT_ID", "KICKOFF_AT"]]]
    ).drop_duplicates("EVENT_ID")
    pre = ticks.merge(kickoffs, on="EVENT_ID", how="left")
    pre = pre[
        pre.KICKOFF_AT.isna()
        | (pd.to_datetime(pre.FIRE_TIME, utc=True) <= pd.to_datetime(pre.KICKOFF_AT, utc=True))
    ]
    latest = (
        pre.sort_values("FIRE_TIME").groupby(JOIN, as_index=False).agg(LATEST_ODDS=("ODDS", "last"))
    )
    legs = legs.merge(latest, on=JOIN, how="left").merge(availability, on=JOIN, how="left")
    ev_all = ev_all.merge(latest, on=JOIN, how="left").merge(availability, on=JOIN, how="left")
    ev_all = ev_all.merge(
        settled[[*JOIN, "DETECTED_AT", "VERDICT"]],
        on=[*JOIN, "DETECTED_AT"],
        how="left",
    )

    # Each tracked market's arbitrage, reduced to its largest moves, keeping the
    # points under every detection so the markers sit on the line.
    tracks: dict[str, Any] = {}
    for (event_id, market_id), history in track.groupby(["EVENT_ID", "MARKET_ID"], sort=False):
        detected = legs[(legs.EVENT_ID == event_id) & (legs.MARKET_ID == market_id)].DETECTED_AT
        priced = history.dropna(subset=["ARBITRAGE"])
        reduced = reduce_steps(
            priced, x="OBSERVED_AT", y="ARBITRAGE", points=POINTS, keep_x=detected
        )
        last = history.iloc[-1]
        tracks[f"{event_id}|{market_id}"] = {
            "total": len(priced),
            "points": _points(reduced, "OBSERVED_AT", "ARBITRAGE", "LEG_SPREAD_SECONDS")
            if not reduced.empty
            else [],
            "last": {
                "at": _iso(last.OBSERVED_AT),
                "arbitrage": _clean(last.ARBITRAGE),
                "spreadSeconds": _clean(last.LEG_SPREAD_SECONDS),
            },
        }

    # Every book's price for each EV outcome, reduced per book, keeping the
    # points under each detection.
    prices: dict[str, Any] = {}
    wanted = ev_all.drop_duplicates(["EVENT_ID", "MARKET_ID", "OUTCOME_ID"])
    by_outcome = {
        key: frame for key, frame in ticks.groupby(["EVENT_ID", "MARKET_ID", "OUTCOME_ID"])
    }
    for row in wanted.itertuples(index=False):
        key = (row.EVENT_ID, row.MARKET_ID, row.OUTCOME_ID)
        series = by_outcome.get(key)
        if series is None:
            continue
        detected = ev_all[
            (ev_all.EVENT_ID == row.EVENT_ID)
            & (ev_all.MARKET_ID == row.MARKET_ID)
            & (ev_all.OUTCOME_ID == row.OUTCOME_ID)
        ].DETECTED_AT
        reduced = reduce_steps(
            series, x="FIRE_TIME", y="ODDS", by=["BOOKMAKER_NAME"], points=POINTS, keep_x=detected
        )
        prices["|".join(key)] = {
            book: _points(part.sort_values("FIRE_TIME"), "FIRE_TIME", "ODDS")
            for book, part in reduced.groupby("BOOKMAKER_NAME")
        }

    return {
        "arbitrage": {
            "legs": _records(
                legs,
                {
                    "SIGNAL_KEY": "signalKey",
                    "EVENT_ID": "eventId",
                    "MARKET_ID": "marketId",
                    "OUTCOME_ID": "outcomeId",
                    "FIXTURE": "fixture",
                    "MARKET_NAME": "market",
                    "LINE": "line",
                    "OUTCOME": "outcome",
                    "BOOKMAKER_NAME": "book",
                    "ODDS": "odds",
                    "URL": "url",
                    "ARBITRAGE": "arbitrage",
                    "SPREAD_SECONDS": "spreadSeconds",
                    "DETECTED_AT": "detectedAt",
                    "KICKOFF_AT": "kickoffAt",
                    "PRE_MATCH": "preMatch",
                    "LATEST_ODDS": "latestOdds",
                    "OFFERED": "offered",
                    "CURRENT_ODDS": "currentOdds",
                    "BOOK_FIRED_AT": "bookFiredAt",
                },
            ),
            "tracks": tracks,
            "stale": {"n": int(stale.N), "worst": _clean(stale.WORST)},
            "efficiency": efficiency,
            "flags": _records(
                flags,
                {
                    "EVENT_ID": "eventId",
                    "MARKET_ID": "marketId",
                    "BOOKMAKER_NAME": "book",
                    "FLAGGED_AT": "flaggedAt",
                },
            ),
        },
        "ev": {
            "rows": _records(
                ev_all,
                {
                    "EVENT_ID": "eventId",
                    "MARKET_ID": "marketId",
                    "OUTCOME_ID": "outcomeId",
                    "FIXTURE": "fixture",
                    "MARKET": "market",
                    "LINE": "line",
                    "OUTCOME_NAME": "outcome",
                    "BOOKMAKER_NAME": "book",
                    "ODDS": "odds",
                    "URL": "url",
                    "IMPLIED_P": "impliedP",
                    "P_SOURCE": "comparable",
                    "P_SOURCE_URL": "comparableUrl",
                    "PROBABILITY_SPREAD_SECONDS": "timeLapseSeconds",
                    "EV": "ev",
                    "KICKOFF_AT": "kickoffAt",
                    "DETECTED_AT": "detectedAt",
                    "IS_FRESH": "isFresh",
                    "LATEST_ODDS": "latestOdds",
                    "OFFERED": "offered",
                    "CURRENT_ODDS": "currentOdds",
                    "BOOK_FIRED_AT": "bookFiredAt",
                    "VERDICT": "verdict",
                },
            ),
            "prices": prices,
            "byBook": _records(
                by_book, {"BOOKMAKER_NAME": "book", "SIGNALS": "signals", "MEAN_EV": "meanEv"}
            ),
            "backtest": backtest(settled),
        },
        "record": record(wh, settled),
    }


def record(wh: Warehouse, settled: pd.DataFrame) -> dict[str, Any]:
    """The track record: a paper wallet placed on every alertable signal, and
    how copied slips fared. What a visitor should see first."""
    surebets = wh.query(
        f"""
        SELECT s.event_id, s.market_id, s.arbitrage, s.detected_at, f.kickoff_at,
               f.home_team || ' v ' || f.away_team AS fixture
        FROM ANALYTICS.stg_arbitrage_signal s
        JOIN CORE.dim_fixture f ON f.event_id = s.event_id
        WHERE s.is_surebet AND s.arbitrage >= {SUREBET_MIN} AND s.detected_at < f.kickoff_at
        QUALIFY row_number() OVER (PARTITION BY s.event_id, s.market_id ORDER BY s.detected_at) = 1
        ORDER BY s.detected_at
        """
    )
    now = pd.Timestamp.now(tz="UTC")
    ev = choose(settled, "first")
    wallet = paper_wallet(surebets, ev, now)
    slips = wh.query(
        """
        SELECT o.share_code, c.followed_times, o.legs, o.won, o.lost, o.combined_odds,
               o.last_kickoff
        FROM ANALYTICS.gold_slip_overview o
        JOIN (
            SELECT share_code, max(followed_times) AS followed_times
            FROM ANALYTICS.gold_slip_leg_history GROUP BY 1
        ) c USING (share_code)
        WHERE o.last_kickoff < current_timestamp AND o.won + o.lost = o.legs AND o.legs > 0
        """
    )
    slips_won = int((slips.LOST == 0).sum()) if not slips.empty else 0
    copies = float(slips.FOLLOWED_TIMES.fillna(0).sum()) if not slips.empty else 0.0
    copies_won = (
        float(slips.loc[slips.LOST == 0, "FOLLOWED_TIMES"].fillna(0).sum())
        if not slips.empty
        else 0.0
    )
    legs = int(slips.LEGS.sum()) if not slips.empty else 0
    return {
        "wallet": {
            "start": 100_000.0,
            "surebetMin": SUREBET_MIN,
            "surebets": wallet.surebets,
            "evBets": wallet.ev_bets,
            "evWon": wallet.ev_won,
            "staked": _clean(wallet.staked),
            "profit": _clean(wallet.profit),
            "final": _clean(wallet.final),
            "maxDrawdown": _clean(wallet.max_drawdown),
            "openBets": wallet.open_bets,
            "from": _iso(surebets.DETECTED_AT.min()) if not surebets.empty else None,
            "curve": _points(wallet.curve, "AT", "BANKROLL"),
        },
        "slips": {
            "settled": int(len(slips)),
            "won": slips_won,
            "legs": legs,
            "legsWon": int(slips.WON.sum()) if not slips.empty else 0,
            "copies": copies,
            "copiesWon": copies_won,
            "medianOdds": _clean(float(slips.COMBINED_ODDS.median())) if not slips.empty else None,
        },
    }


def backtest(settled: pd.DataFrame) -> dict[str, Any]:
    """Every timing x sizing, precomputed: the page only switches between them."""
    done = settled.dropna(subset=["VERDICT"])
    opportunities = done.drop_duplicates(["EVENT_ID", "MARKET_ID", "OUTCOME_ID"])
    if opportunities.empty:
        return {"opportunities": 0, "table": [], "curves": {}}
    table = compare(settled)
    curves = {
        timing: {
            sizing: _points(simulate(choose(settled, timing), sizing).curve, "AT", "BANKROLL")
            for sizing in SIZINGS
        }
        for timing in TIMINGS
    }
    return {
        "opportunities": len(opportunities),
        "from": _iso(pd.to_datetime(done.KICKOFF_AT, utc=True).min()),
        "to": _iso(pd.to_datetime(done.KICKOFF_AT, utc=True).max()),
        "table": [_clean(r) for r in table.to_dict(orient="records")],
        "curves": curves,
    }


# --- live: slips -----------------------------------------------------------------------


def rank_slips(cards: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """Order slip cards the way a punter holding them would.

    Upcoming (last leg still to kick off), first every slip still ALIVE -- no
    leg lost -- by how many legs it has won, then by what those legs have
    locked in (the multiplier they secured), then the fewest legs still to
    land, then copies. Two legs won and one to play beats a slip nothing has
    settled on yet. Slips with a
    leg already lost are dead whatever else they hold; they come after every
    live one, by copies, so the reader sees them but never above a live one.
    Played slips stay by copies: they are the record, not a position.

    Adds ALIVE, REMAINING and RANK; sorts by RANK.
    """
    cards = cards.copy()
    won = cards.WON.fillna(0).astype(int)
    lost = cards.LOST.fillna(0).astype(int)
    legs = cards.LEG_COUNT.fillna(cards.LEGS).fillna(0).astype(int)
    copies = cards.FOLLOWED_TIMES.fillna(0).astype(float)
    cards["ALIVE"] = lost == 0
    cards["REMAINING"] = (legs - won - lost).clip(lower=0)
    upcoming = pd.to_datetime(cards.LAST_KICKOFF, utc=True) > now
    locked = cards.LOCKED_IN.fillna(1.0).astype(float) if "LOCKED_IN" in cards else 1.0
    order = pd.DataFrame(
        {
            "played": ~upcoming,
            "dead": ~cards.ALIVE,
            "won": -won,
            "locked": -locked,
            "remaining": cards.REMAINING,
            "copies": -copies,
        },
        index=cards.index,
    )
    cards = cards.loc[order.sort_values(list(order.columns), kind="stable").index]
    cards["RANK"] = range(1, len(cards) + 1)
    return cards


def slips(wh: Warehouse) -> dict[str, Any]:
    popular = wh.query(
        """
        SELECT p.event_id, f.home_team || ' v ' || f.away_team AS fixture,
               f.tournament, f.kickoff_at, p.slips, p.follows
        FROM ANALYTICS.gold_fixture_popularity p
        JOIN CORE.dim_fixture f ON f.event_id = p.event_id
        ORDER BY p.slips DESC
        """
    )
    cards = wh.query(
        """
        SELECT s.share_code, s.followed_times, s.legs, s.legs_with_history, s.summary,
               o.legs AS leg_count, o.first_kickoff, o.last_kickoff, o.won, o.lost,
               o.combined_odds, o.locked_in, o.pending_odds
        FROM CORE.gold_slip_summary_ai s
        LEFT JOIN ANALYTICS.gold_slip_overview o ON o.share_code = s.share_code
        QUALIFY row_number() OVER (PARTITION BY s.share_code ORDER BY s.generated_at DESC) = 1
        ORDER BY s.followed_times DESC NULLS LAST
        """
    )
    waiting = int(
        wh.query(
            """
            SELECT count(*) AS n
            FROM ANALYTICS.gold_slip_overview o
            WHERE o.last_kickoff > current_timestamp
              AND o.share_code NOT IN (SELECT share_code FROM CORE.gold_slip_summary_ai)
            """
        )
        .iloc[0]
        .N
    )
    now = pd.Timestamp.now(tz="UTC")
    cards = rank_slips(cards, now)
    upcoming = pd.to_datetime(cards.LAST_KICKOFF, utc=True) > now
    shown = pd.concat([cards[upcoming].head(SLIP_CARDS), cards[~upcoming].head(SLIP_CARDS)])
    codes = list(shown.SHARE_CODE)
    picks = _picks(wh)
    legs = (
        wh.query(
            f"""
            SELECT g.share_code, g.event_id, g.home_team, g.away_team, g.market_name,
                   g.outcome_name, g.odds, g.kickoff_at, g.history_wins, g.history_matches,
                   g.resolution, u.url, e.match_status, e.score, e.played_time
            FROM ANALYTICS.gold_slip_leg_history g
            LEFT JOIN ANALYTICS.stg_event_link u
              ON u.event_id = g.event_id AND u.bookmaker_name = 'msport'
            LEFT JOIN CORE.fact_event_state e ON e.event_id = g.event_id
            WHERE g.share_code IN ({_quoted(codes)})
            ORDER BY g.share_code, g.leg_index
            """
        )
        if codes
        else pd.DataFrame()
    )
    leg_columns = {
        "EVENT_ID": "eventId",
        "HOME_TEAM": "home",
        "AWAY_TEAM": "away",
        "MARKET_NAME": "market",
        "OUTCOME_NAME": "pick",
        "ODDS": "odds",
        "KICKOFF_AT": "kickoffAt",
        "HISTORY_WINS": "historyWins",
        "HISTORY_MATCHES": "historyMatches",
        "RESOLUTION": "resolution",
        "URL": "url",
        "MATCH_STATUS": "matchStatus",
        "SCORE": "score",
        "PLAYED_TIME": "playedTime",
    }
    return {
        "popular": _records(
            popular,
            {
                "EVENT_ID": "eventId",
                "FIXTURE": "fixture",
                "TOURNAMENT": "tournament",
                "KICKOFF_AT": "kickoffAt",
                "SLIPS": "slips",
                "FOLLOWS": "follows",
            },
        ),
        "cards": _records(
            shown,
            {
                "SHARE_CODE": "shareCode",
                "FOLLOWED_TIMES": "followedTimes",
                "LEGS": "legs",
                "LEGS_WITH_HISTORY": "legsWithHistory",
                "SUMMARY": "summary",
                "LEG_COUNT": "legCount",
                "FIRST_KICKOFF": "firstKickoff",
                "LAST_KICKOFF": "lastKickoff",
                "WON": "won",
                "LOST": "lost",
                "COMBINED_ODDS": "combinedOdds",
                "LOCKED_IN": "lockedIn",
                "PENDING_ODDS": "pendingOdds",
                "ALIVE": "alive",
                "REMAINING": "remaining",
                "RANK": "rank",
            },
        ),
        "legs": {
            code: _records(part, leg_columns)
            for code, part in (legs.groupby("SHARE_CODE") if not legs.empty else [])
        },
        "waiting": waiting,
        "picks": picks,
    }


# One pick = one outcome of one match, as it appeared across every slip that
# carried it. Copies are summed over those slips, so a pick on a hundred
# small slips and a pick on one huge slip compare on the same footing.
_PICKS = """
    WITH pick AS (
        SELECT event_id, market_name, outcome_name, share_code,
               any_value(home_team || ' v ' || away_team) AS fixture,
               any_value(tournament) AS tournament,
               any_value(kickoff_at) AS kickoff_at,
               any_value(odds) AS odds,
               any_value(followed_times) AS copies,
               any_value(resolution) AS resolution
        FROM ANALYTICS.gold_slip_leg_history
        WHERE resolution IN ('won', 'lost') AND odds > 1
        GROUP BY 1, 2, 3, 4
    ),
    agg AS (
        SELECT event_id, market_name, outcome_name, resolution,
               any_value(fixture) AS fixture, any_value(tournament) AS tournament,
               any_value(kickoff_at) AS kickoff_at,
               median(odds) AS odds, count(*) AS slips, sum(coalesce(copies, 0)) AS copies
        FROM pick GROUP BY 1, 2, 3, 4
    )
    SELECT * FROM agg WHERE resolution = '{verdict}' AND copies > 0
    ORDER BY {score} DESC LIMIT {limit}
"""


def _picks(wh: Warehouse) -> dict[str, list[dict[str, Any]]]:
    """The biggest winning picks (most copies at the longest price) and the
    biggest losing ones (most copies at the shortest price: the sure things
    that did not land)."""
    columns = {
        "EVENT_ID": "eventId",
        "FIXTURE": "fixture",
        "TOURNAMENT": "tournament",
        "KICKOFF_AT": "kickoffAt",
        "MARKET_NAME": "market",
        "OUTCOME_NAME": "pick",
        "ODDS": "odds",
        "SLIPS": "slips",
        "COPIES": "copies",
        "RESOLUTION": "resolution",
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for verdict, score in (("won", "copies * odds"), ("lost", "copies / odds")):
        sql = _PICKS.format(verdict=verdict, score=score, limit=PICKS)
        out[verdict] = _records(wh.query(sql), columns)
    return out


# --- deep dives ------------------------------------------------------------------------

FORM_COLUMNS = {
    "MATCH_DATE": "date",
    "LEAGUE_NAME": "competition",
    "OPPONENT_NAME": "opponent",
    "IS_HOME": "isHome",
    "RESULT": "result",
    "GOALS_FOR": "goalsFor",
    "GOALS_AGAINST": "goalsAgainst",
    "XG": "xg",
    "XG_AGAINST": "xga",
    "POSSESSION": "possession",
    "CORNERS": "corners",
    "TOTAL_SHOTS": "shots",
    "SHOTS_ON": "shotsOn",
    "PASSES_TOTAL": "passes",
    "PASSES_ACCURATE": "passesAccurate",
    "PASSES_PCT": "passesPct",
    "SAVES": "saves",
    "BLOCKED_SHOTS": "blocked",
    "FOULS": "fouls",
    "YELLOW": "yellow",
    "RED": "red",
}


def dives(wh: Warehouse, event_ids: list[str]) -> dict[str, Any]:
    """Deep dives for many fixtures, in a handful of batched queries."""
    if not event_ids:
        return {}
    ids = _quoted(event_ids)
    fixtures = wh.query(
        f"""
        SELECT event_id, home_team, away_team, tournament, kickoff_at,
               home_team_id, away_team_id
        FROM CORE.dim_fixture WHERE event_id IN ({ids})
        """
    )
    if fixtures.empty:
        return {}
    briefs = wh.query(
        f"""
        SELECT event_id, summary, model, markets_described, sides_with_history, generated_at
        FROM CORE.gold_fixture_summary_ai
        WHERE event_id IN ({ids})
        QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY generated_at DESC) = 1
        """
    ).set_index("EVENT_ID")
    results = wh.query(
        f"""
        SELECT event_id, summary, model, generated_at
        FROM CORE.gold_fixture_result_ai WHERE event_id IN ({ids})
        """
    ).set_index("EVENT_ID")
    # The busiest markets per fixture, then only their ticks.
    ticks = wh.query(
        f"""
        WITH busiest AS (
            SELECT event_id, market_id, count(*) AS n
            FROM ANALYTICS.stg_odds_tick
            WHERE event_id IN ({ids})
            GROUP BY 1, 2
            QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY count(*) DESC, market_id)
                <= {DIVE_MARKETS}
        )
        SELECT t.event_id, t.market_id, t.market_name, t.line, t.outcome, t.bookmaker_name,
               t.odds, t.fire_time, b.n
        FROM ANALYTICS.stg_odds_tick t
        JOIN busiest b ON b.event_id = t.event_id AND b.market_id = t.market_id
        """
    )
    teams = fixtures.dropna(subset=["HOME_TEAM_ID", "AWAY_TEAM_ID"])
    wanted = [
        (int(team), pd.Timestamp(f.KICKOFF_AT))
        for f in teams.itertuples(index=False)
        for team in (f.HOME_TEAM_ID, f.AWAY_TEAM_ID)
    ]
    form = pd.DataFrame()
    market_record = pd.DataFrame()
    if wanted:
        values = ", ".join(f"({team}, '{before:%Y-%m-%d}'::DATE)" for team, before in set(wanted))
        form = wh.query(
            f"""
            WITH wanted (team_id, before) AS (VALUES {values})
            SELECT w.team_id, w.before, m.match_date, m.league_name, m.opponent_name,
                   m.is_home, m.result, m.goals_for, m.goals_against, m.xg, m.xg_against,
                   m.possession, m.corners, m.total_shots, m.shots_on, m.passes_total,
                   m.passes_accurate, m.passes_pct, m.saves, m.blocked_shots, m.fouls,
                   m.yellow, m.red
            FROM wanted w
            JOIN CORE.fact_team_match m
              ON m.team_id = w.team_id AND m.match_date < w.before
            QUALIFY row_number() OVER (
                PARTITION BY w.team_id, w.before ORDER BY m.match_date DESC
            ) <= {FORM_WINDOW}
            """
        )
        market_record = wh.query(
            f"""
            WITH wanted (team_id, before) AS (VALUES {values}),
            recent AS (
                SELECT w.team_id, w.before, r.market_family, r.period, r.side_or_line,
                       r.verdict,
                       row_number() OVER (
                           PARTITION BY w.team_id, w.before, r.market_family, r.period,
                                        r.side_or_line
                           ORDER BY r.match_date DESC
                       ) AS recency
                FROM wanted w
                JOIN CORE.fact_team_market_result r
                  ON r.team_id = w.team_id AND r.match_date < w.before
            )
            SELECT team_id, before, market_family, period, side_or_line,
                   count(*) AS matches,
                   sum(CASE WHEN verdict = 'won' THEN 1 ELSE 0 END) AS landed
            FROM recent
            WHERE recency <= {FORM_WINDOW}
            GROUP BY 1, 2, 3, 4, 5
            HAVING count(*) >= 5
            """
        )
    picks = wh.query(
        f"""
        SELECT event_id, share_code, followed_times, market_name, outcome_name, odds,
               history_wins, history_matches, resolution
        FROM ANALYTICS.gold_slip_leg_history
        WHERE event_id IN ({ids})
        """
    )

    ticks_by_event = dict(tuple(ticks.groupby("EVENT_ID"))) if not ticks.empty else {}
    picks_by_event = dict(tuple(picks.groupby("EVENT_ID"))) if not picks.empty else {}
    out: dict[str, Any] = {}
    for f in fixtures.itertuples(index=False):
        kickoff = pd.Timestamp(f.KICKOFF_AT)
        dive: dict[str, Any] = {
            "eventId": f.EVENT_ID,
            "home": f.HOME_TEAM,
            "away": f.AWAY_TEAM,
            "tournament": f.TOURNAMENT,
            "kickoffAt": _iso(kickoff),
            "hasTeams": bool(pd.notna(f.HOME_TEAM_ID) and pd.notna(f.AWAY_TEAM_ID)),
            "brief": None,
            "result": None,
            "markets": [],
            "teams": [],
            "settled": [],
            "punters": None,
        }
        if f.EVENT_ID in briefs.index:
            b = briefs.loc[f.EVENT_ID]
            dive["brief"] = _clean(
                {
                    "summary": b.SUMMARY,
                    "model": b.MODEL,
                    "markets": b.MARKETS_DESCRIBED,
                    "sides": b.SIDES_WITH_HISTORY,
                    "generatedAt": _iso(b.GENERATED_AT),
                }
            )
        if f.EVENT_ID in results.index:
            r = results.loc[f.EVENT_ID]
            dive["result"] = {
                "summary": r.SUMMARY,
                "model": r.MODEL,
                "generatedAt": _iso(r.GENERATED_AT),
            }
        event_ticks = ticks_by_event.get(f.EVENT_ID)
        if event_ticks is not None:
            for market_id, market in sorted(
                # Ties broken by id: the snapshot must be identical build to build, or
                # the serving publisher sees a change that is not one.
                event_ticks.groupby("MARKET_ID"),
                key=lambda kv: (-int(kv[1].N.iloc[0]), kv[0]),
            ):
                reduced = reduce_steps(
                    market, x="FIRE_TIME", y="ODDS", by=["OUTCOME", "BOOKMAKER_NAME"], points=POINTS
                )
                series: dict[str, dict[str, Any]] = {}
                for (outcome, book), part in reduced.groupby(["OUTCOME", "BOOKMAKER_NAME"]):
                    series.setdefault(str(outcome), {})[book] = _points(
                        part.sort_values("FIRE_TIME"), "FIRE_TIME", "ODDS"
                    )
                head = market.iloc[0]
                dive["markets"].append(
                    {
                        "marketId": market_id,
                        "name": head.MARKET_NAME,
                        "line": head.LINE,
                        "ticks": int(head.N),
                        "books": int(market.BOOKMAKER_NAME.nunique()),
                        "series": series,
                    }
                )
        if dive["hasTeams"]:
            # The same day string the VALUES list was built from.
            before = f"{kickoff:%Y-%m-%d}"
            for team_id, name, role in (
                (int(f.HOME_TEAM_ID), f.HOME_TEAM, "home"),
                (int(f.AWAY_TEAM_ID), f.AWAY_TEAM, "away"),
            ):
                mine = (
                    form[
                        (form.TEAM_ID == team_id) & (form.BEFORE.astype(str) == before)
                    ].sort_values("MATCH_DATE", ascending=False)
                    if not form.empty
                    else form
                )
                dive["teams"].append(
                    {"name": name, "role": role, "matches": _records(mine, FORM_COLUMNS)}
                )
                record = (
                    market_record[
                        (market_record.TEAM_ID == team_id)
                        & (market_record.BEFORE.astype(str) == before)
                    ]
                    if not market_record.empty
                    else market_record
                )
                dive["settled"] += [
                    {**row, "side": name}
                    for row in _records(
                        record,
                        {
                            "MARKET_FAMILY": "market",
                            "PERIOD": "period",
                            "SIDE_OR_LINE": "pick",
                            "LANDED": "landed",
                            "MATCHES": "of",
                        },
                    )
                ]
            dive["settled"] = sorted(
                dive["settled"],
                key=lambda s: (
                    -s["landed"] / s["of"],
                    -s["of"],
                    s["side"],
                    s["market"],
                    s["period"],
                    s["pick"],
                ),
            )[:40]
        event_picks = picks_by_event.get(f.EVENT_ID)
        if event_picks is not None:
            dive["punters"] = punters(event_picks)
        out[f.EVENT_ID] = dive
    return out


def punters(picks: pd.DataFrame) -> dict[str, Any]:
    per_slip = picks.drop_duplicates("SHARE_CODE")
    copies = per_slip.FOLLOWED_TIMES.fillna(0)
    by_pick = (
        picks.assign(PICK=picks.MARKET_NAME + " · " + picks.OUTCOME_NAME)
        .groupby("PICK", as_index=False)
        .agg(
            SLIPS=("SHARE_CODE", "nunique"),
            COPIES=("FOLLOWED_TIMES", "sum"),
            MEDIAN_ODDS=("ODDS", "median"),
            WINS=("HISTORY_WINS", "first"),
            MATCHES=("HISTORY_MATCHES", "first"),
            RESULT=("RESOLUTION", "first"),
        )
        .sort_values(["SLIPS", "COPIES", "PICK"], ascending=[False, False, True])
    )
    return {
        "slips": len(per_slip),
        "copies": int(copies.sum()),
        "medianCopies": _clean(float(copies.median())),
        "picks": _records(
            by_pick,
            {
                "PICK": "pick",
                "SLIPS": "slips",
                "COPIES": "copies",
                "MEDIAN_ODDS": "medianOdds",
                "WINS": "wins",
                "MATCHES": "matches",
                "RESULT": "result",
            },
        ),
    }


# --- publishing ---------------------------------------------------------------------------


def _berlin_day(value: Any) -> str:
    return pd.Timestamp(value).tz_convert(TIMEZONE).strftime("%Y-%m-%d")


class Publisher:
    def __init__(self) -> None:
        self.token = os.environ.get("BLOB_READ_WRITE_TOKEN")
        # What has been UPLOADED is tracked apart from what was only written
        # locally, or the first run with a token would skip every file a
        # tokenless development run had already hashed.
        self.state_path = OUT / ("state.json" if self.token else "state.local.json")
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.state = {"files": {}, "archived": []}
        self.uploaded = 0

    def write(self, name: str, payload: dict[str, Any], *, digest_of: Any = None) -> bool:
        """Write locally; upload only if the content changed. True if uploaded."""
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(
            json.dumps(digest_of, sort_keys=True, default=str).encode("utf-8")
            if digest_of is not None
            else body
        ).hexdigest()
        path = OUT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        compressed = gzip.compress(body, mtime=0)
        path.write_bytes(compressed)
        if self.state["files"].get(name) == digest:
            log.info("%s unchanged (%d KB), not uploaded", name, len(compressed) // 1024)
            return False
        if self.token:
            _blob_put(self.token, name, compressed)
            self.uploaded += 1
            log.info("%s uploaded (%d KB)", name, len(compressed) // 1024)
        else:
            log.info("%s written locally (%d KB), no token", name, len(compressed) // 1024)
        self.state["files"][name] = digest
        return True

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=1), encoding="utf-8")

    def revalidate(self) -> None:
        url = os.environ.get("WEB_REVALIDATE_URL")
        secret = os.environ.get("WEB_REVALIDATE_SECRET")
        if not (url and secret and self.uploaded):
            return
        try:
            response = httpx.post(url, headers={"x-revalidate-secret": secret}, timeout=20)
            log.info("revalidate: %s", response.status_code)
        except httpx.HTTPError as err:
            # The pages still refresh on their hourly fallback.
            log.warning("revalidate failed: %s", err)


BLOB_API = "https://vercel.com/api/blob"
# What @vercel/blob 2.8 sends (dist/chunk-*.js: BLOB_API_VERSION, createPutHeaders).
BLOB_API_VERSION = "12"


def _blob_put(token: str, pathname: str, body: bytes) -> None:
    """Vercel Blob's PUT, as `@vercel/blob`'s `put()` sends it.

    Fixed pathname (no random suffix) and overwrite allowed, so each file keeps
    one URL. A one-minute CDN cache: an overwritten file must not be served
    stale for the SDK default of a month.
    """
    # vercel_blob_rw_<storeId>_<secret>
    store_id = token.split("_")[3]
    response = httpx.put(
        BLOB_API + "/?" + urlencode({"pathname": pathname}),
        content=body,
        headers={
            "authorization": f"Bearer {token}",
            "x-api-version": BLOB_API_VERSION,
            "x-vercel-blob-store-id": store_id,
            "x-content-length": str(len(body)),
            "x-vercel-blob-access": "public",
            "x-add-random-suffix": "0",
            "x-allow-overwrite": "1",
            "x-cache-control-max-age": "60",
            "x-content-type": "application/gzip",
        },
        timeout=120,
    )
    response.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--archive-recent",
        action="store_true",
        help="rewrite the last three archived days, for post-match notes that came late",
    )
    args = parser.parse_args()

    publisher = Publisher()
    wh = Warehouse()
    try:
        now = pd.Timestamp.now(tz="UTC")
        live: dict[str, Any] = {"generatedAt": _iso(now)}
        live |= headline(wh)
        live |= signals(wh)
        live["slips"] = slips(wh)

        popular = pd.DataFrame(live["slips"]["popular"])
        if popular.empty:
            popular = pd.DataFrame(columns=["eventId", "kickoffAt", "slips"])
        popular["kickoff"] = pd.to_datetime(popular.kickoffAt, utc=True)
        popular = popular.dropna(subset=["kickoff"])
        popular["day"] = popular.kickoff.map(_berlin_day)

        # A day is archivable once its last slipped match is three hours old.
        last_by_day = popular.groupby("day").kickoff.max()
        complete = sorted(day for day, last in last_by_day.items() if last + SETTLE_DELAY < now)
        archived = set(publisher.state.get("archived", []))
        todo = [d for d in complete if d not in archived]
        if args.archive_recent:
            todo = sorted(set(todo) | set(complete[-3:]))
        for day in todo:
            ids = list(popular[popular.day == day].eventId)
            payload = {"day": day, "generatedAt": _iso(now), "dives": dives(wh, ids)}
            publisher.write(
                f"archive/{day}.json.gz",
                payload,
                digest_of={"day": day, "dives": payload["dives"]},
            )
            archived.add(day)
            log.info("archive %s: %d fixtures", day, len(ids))
        publisher.state["archived"] = sorted(archived)

        # Live deep dives: upcoming, and played on a day not archived yet.
        upcoming = popular[popular.kickoff > now].head(UPCOMING_DIVES)
        recent = popular[(popular.kickoff <= now) & ~popular.day.isin(archived)].head(RECENT_DIVES)
        live["dives"] = dives(wh, list(upcoming.eventId) + list(recent.eventId))
        live["archivedDays"] = sorted(archived)
        # generatedAt changes every run; hash the content without it, so an
        # unchanged warehouse is not an upload.
        publisher.write(
            "live.json.gz", live, digest_of={k: v for k, v in live.items() if k != "generatedAt"}
        )
    finally:
        wh.close()
        publisher.save()
    publisher.revalidate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
