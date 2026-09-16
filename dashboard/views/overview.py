"""Cross-bookmaker market signals: the dashboard's front page.

Warehouse access, credentials and the platform timezone live in
`dashboard/common.py`; this file is only the page. Booking slips and deep dives
live on their own page, `views/slips.py`.

Every section below the headline numbers is collapsible, so the page can be
navigated rather than scrolled. Arbitrage and positive EV each have an Upcoming
and a Past tab: what can still be bet, and what the record says.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st
from dashboard.arbitrage import stake_split
from dashboard.backtest import SIZINGS, TIMINGS, choose, compare, simulate
from dashboard.charts import arbitrage_chart, book_bars, efficiency_bars, odds_chart
from dashboard.common import (
    execute,
    is_upcoming,
    kickoff,
    query,
    register_warmer,
    warehouse_written,
    watermark,
)
from dashboard.series import MAX_LEG_SPREAD_SECONDS, POINTS

st.title("Arbibet — cross-bookmaker market signals")
st.caption(
    "Five Nigerian and international bookmakers, reconciled onto one market "
    "taxonomy. Every number below is measured, and the ones that mean less "
    "than they look are labelled."
)

# --- as of -----------------------------------------------------------------
# Every source under this page rolls: bronze prunes, the fixture loader moves
# its window, the producer runs again. A page with no date on it silently
# claims to be current. So it says when its newest input arrived.
asof = query(
    """
    SELECT
      -- ::TIMESTAMP_LTZ renders in session time (Europe/Berlin). These are
      -- TIMESTAMP_TZ columns, which keep the offset their WRITER used and
      -- ignore the session setting -- see dbt/macros/platform_time.sql.
      (SELECT max(detected_at)::TIMESTAMP_LTZ  FROM CORE.fact_arbitrage_signal) AS last_signal,
      (SELECT max(generated_at)::TIMESTAMP_LTZ FROM CORE.gold_slip_summary_ai)  AS last_summary,
      (SELECT max(fire_time)::TIMESTAMP_LTZ    FROM CORE.fact_odds_tick)        AS last_tick
    """
).iloc[0]
st.caption(
    f"Newest signal {asof.LAST_SIGNAL:%Y-%m-%d %H:%M} · "
    f"newest price {asof.LAST_TICK:%Y-%m-%d %H:%M} · "
    f"newest slip verdict {asof.LAST_SUMMARY:%Y-%m-%d %H:%M} · "
    f"warehouse last written {warehouse_written()}"
)
st.caption(
    "This page refreshes when the warehouse is written, not on a timer: every "
    "query is keyed on Snowflake's own last-write time, so a pipeline run "
    "invalidates it and nothing else does."
)

# --- headline numbers ------------------------------------------------------
# Only TRUE surebets are counted. The consumer records down to 0.98 so the
# near misses are available for measuring market efficiency, but a headline
# that counts them reads as "25 opportunities" when there were four.
# Surebets are counted only where their legs were priced within five minutes
# of each other. 48 recorded "surebets" -- up to 1.6788, a 68% guaranteed
# return -- came from prices up to hours apart: each real, never on sale at the
# same moment. Every one had legs more than five minutes apart and none inside
# it (FINDINGS 13g). `is_surebet` carries that rule, and the detector now
# refuses such sets outright.
counts = query(
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

a, b, c, d, e = st.columns(5)
a.metric(
    "True surebets",
    f"{counts.SUREBETS:,}",
    help=(
        "arbitrage > 1, priced before kick-off, every leg within five minutes "
        "of the others. All time — the ones still placeable are under "
        "Upcoming below. Genuine surebets across public books barely exist."
    ),
)
b.metric(
    "Positive-EV",
    f"{counts.EV_SIGNALS:,}",
    help="Priced before kick-off. All time; upcoming ones are listed below.",
)
c.metric(
    "Price changes",
    f"{counts.TICKS:,}",
    help="Every published price move, replayed from bronze.",
)
d.metric(
    "Markets settled",
    f"{counts.SETTLED:,}",
    help="Historical results, per team per market.",
)
e.metric("Slips analysed", f"{counts.SLIPS:,}")

# --- section 1: how an arbitrage is calculated -------------------------------
with st.expander("How an arbitrage is calculated", expanded=False):
    st.markdown(
        """
**The number.** For one market, take the **best price per outcome across every
book**, then

> arbitrage = 1 / Σ (1 / best odds)

Above **1.0** is a surebet: backing every outcome at those prices returns more
than the stake whatever happens. `1 / arbitrage − 1` is the **overround** left
after shopping every book — the market-efficiency score below.

**The stake.** To be paid the same whichever outcome lands, put

> stake on outcome *i* = (1 / odds*ᵢ*) / Σ (1 / odds)

of the total on each. Every outcome then returns *stake × arbitrage*. The
surebet cards below do this for you, at odds you can edit.

**What the detector refuses**, each rule learned from a number that looked
real and was not:

- **Legs from one book.** Two outcomes of one book are that book's own margin,
  not a disagreement between books.
- **Legs priced more than five minutes apart.** A snapshot holds each book's
  *last published* price, so one can be hours behind another (see below).
- **Suspended markets.** msport flags suspension on the market while leaving
  every outcome "active"; those prices are skipped.
- **Kicked-off fixtures.** In play, books suspend and resume at different
  moments.
- **Markets without a usable probability** (more than three outcomes, or
  published probabilities summing below 0.95).
"""
    )

    example = query(
        """
        SELECT fixture, market_name, line, outcome, bookmaker_name, odds, arbitrage,
               spread_seconds, detected_at
        FROM ANALYTICS.stg_arbitrage_leg
        WHERE signal_key = (
            SELECT signal_key FROM ANALYTICS.stg_arbitrage_signal
            WHERE is_surebet ORDER BY detected_at DESC LIMIT 1
        )
        """
    )
    if not example.empty:
        head = example.iloc[0]
        line = f" {head.LINE}" if head.LINE else ""
        split = stake_split(list(example.ODDS))
        st.markdown(
            f"**Worked example — the newest surebet:** {head.FIXTURE}, "
            f"{head.MARKET_NAME}{line}, priced {head.DETECTED_AT:%d %b %H:%M}, "
            f"legs {int(head.SPREAD_SECONDS)}s apart."
        )
        worked = example.assign(
            INVERSE=1 / example.ODDS,
            STAKE=split.fractions if split else None,
        )
        st.dataframe(
            worked[["OUTCOME", "BOOKMAKER_NAME", "ODDS", "INVERSE", "STAKE"]].rename(
                columns={
                    "OUTCOME": "Outcome",
                    "BOOKMAKER_NAME": "Best book",
                    "ODDS": "Odds",
                    "INVERSE": "1 / odds",
                    "STAKE": "Stake share",
                }
            ),
            column_config={
                "1 / odds": st.column_config.NumberColumn(format="%.4f"),
                "Stake share": st.column_config.ProgressColumn(
                    min_value=0.0, max_value=1.0, format="percent"
                ),
            },
            hide_index=True,
            use_container_width=True,
        )
        st.caption(
            f"Σ 1/odds = {worked.INVERSE.sum():.4f}, so arbitrage = "
            f"1 / {worked.INVERSE.sum():.4f} = **{1 / worked.INVERSE.sum():.4f}**: "
            f"{(1 / worked.INVERSE.sum() - 1):+.2%} on the total stake, whatever wins."
        )

    st.markdown("**How simultaneous is a 'snapshot'?**")
    st.caption(
        "A snapshot holds each book's LAST published price, so one book can be "
        "an hour behind another. An arbitrage across a fresh leg and an hours-old "
        "one is an artefact, not an opportunity. Measured here: no signal with "
        "legs inside five minutes was ever implausible, and all 48 implausible "
        "ones sat above it, so the detector now refuses anything wider. The "
        "older rows below predate that rule."
    )
    freshness = query(
        """
        SELECT
          CASE
            WHEN leg_spread_seconds <= 60    THEN 'under 1 min'
            WHEN leg_spread_seconds <= 300   THEN '1-5 min'
            WHEN leg_spread_seconds <= 3600  THEN '5-60 min'
            ELSE 'over an hour'
          END                                                 AS spread,
          count(*)                                            AS signals,
          sum(CASE WHEN is_surebet THEN 1 ELSE 0 END)         AS surebets
        FROM ANALYTICS.stg_arbitrage_signal
        GROUP BY 1
        ORDER BY min(leg_spread_seconds)
        """
    )
    if not freshness.empty:
        fresh_left, fresh_right = st.columns([2, 3])
        fresh_left.dataframe(freshness, hide_index=True, use_container_width=True)
        fresh_right.bar_chart(freshness.set_index("SPREAD")[["SIGNALS"]], height=240)

# --- data, prepared once per warehouse write ------------------------------------
# Every query and merge the signal sections need, cached against the warehouse
# watermark. The first version rebuilt all of it on every rerun -- a slider
# move, a tab switch, navigating back from another page -- and drew a chart per
# card, 38 in all: about six seconds a visit with every query already cached.
# `is_upcoming` is deliberately NOT applied in here: it depends on the clock,
# and a cached answer would keep a kicked-off match "upcoming" until the next
# pipeline write.


@st.cache_data(show_spinner=False)
def _prepare(mark: str) -> dict[str, pd.DataFrame]:
    del mark  # the cache key; the queries below read the same watermark
    surebet_legs = query(
        """
        SELECT l.signal_key, l.event_id, l.market_id, l.outcome_id,
               l.fixture, l.market_name, l.line, l.outcome,
               l.bookmaker_name, l.odds, u.url, l.arbitrage, l.spread_seconds,
               l.detected_at, l.kickoff_at, l.n_legs,
               l.detected_at < l.kickoff_at AS pre_match
        FROM ANALYTICS.stg_arbitrage_leg l
        LEFT JOIN ANALYTICS.stg_event_link u
          ON u.event_id = l.event_id AND u.bookmaker_name = l.bookmaker_name
        WHERE l.is_surebet
        ORDER BY l.arbitrage DESC, l.fixture, l.outcome
        """
    )
    # Where every market that ever carried a surebet has stood since, rebuilt by
    # replaying bronze through the detector (odds/arbitrage_track.py).
    track = query(
        """
        SELECT event_id, market_id, observed_at, arbitrage, leg_spread_seconds
        FROM ANALYTICS.stg_arbitrage_track
        ORDER BY observed_at
        """
    )
    ev_all = query(
        """
        SELECT
          s.event_id, s.market_id, s.outcome_id,
          COALESCE(f.home_team || ' v ' || f.away_team, s.event_id) AS fixture,
          COALESCE(m.market_name, 'market ' || s.market_base_id)    AS market,
          s.specifier AS line, s.outcome_name, s.bookmaker_name,
          s.odds, u.url, s.implied_p, s.p_source, s.probability_spread_seconds, s.ev,
          f.kickoff_at::TIMESTAMP_LTZ AS kickoff_at, s.detected_at, s.is_fresh
        FROM ANALYTICS.stg_ev_signal s
        LEFT JOIN CORE.dim_fixture f ON f.event_id = s.event_id
        LEFT JOIN ANALYTICS.stg_event_link u
          ON u.event_id = s.event_id AND u.bookmaker_name = s.bookmaker_name
        LEFT JOIN CORE.dim_market m ON m.market_base_id = s.market_base_id
        WHERE s.detected_at < f.kickoff_at
        ORDER BY s.ev DESC
        """
    )
    signal_ticks = query(
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
    # Is each leg still in the book's LATEST payload (odds/live_state.py).
    availability = query(
        """
        SELECT a.event_id, a.market_id, a.outcome_id, b.bookmaker_name,
               a.offered, a.current_odds, a.book_fired_at::TIMESTAMP_LTZ AS book_fired_at
        FROM CORE.fact_leg_availability a
        JOIN CORE.dim_bookmaker b USING (bookmaker_id)
        """
    )
    flags = query(
        """
        SELECT event_id, market_id, bookmaker_name, flagged_at::TIMESTAMP_LTZ AS flagged_at
        FROM CORE.dashboard_leg_flag
        WHERE active
        """
    )
    for frame in (surebet_legs, ev_all, signal_ticks, availability):
        frame["OUTCOME_ID"] = frame.OUTCOME_ID.astype(str)

    # Latest price per (market, outcome, book), as of kick-off for a played match.
    kickoffs = pd.concat(
        [surebet_legs[["EVENT_ID", "KICKOFF_AT"]], ev_all[["EVENT_ID", "KICKOFF_AT"]]]
    ).drop_duplicates("EVENT_ID")
    pre = signal_ticks.merge(kickoffs, on="EVENT_ID", how="left")
    pre = pre[
        pre.KICKOFF_AT.isna()
        | (pd.to_datetime(pre.FIRE_TIME, utc=True) <= pd.to_datetime(pre.KICKOFF_AT, utc=True))
    ]
    latest = (
        pre.sort_values("FIRE_TIME").groupby(JOIN, as_index=False).agg(LATEST_ODDS=("ODDS", "last"))
    )
    surebet_legs = surebet_legs.merge(latest, on=JOIN, how="left").merge(
        availability, on=JOIN, how="left"
    )
    ev_all = ev_all.merge(latest, on=JOIN, how="left").merge(availability, on=JOIN, how="left")
    for frame in (surebet_legs, ev_all):
        frame["OFFERED_NOW"] = frame.apply(_offered_text, axis=1) if not frame.empty else []
    ev_all["FRESHNESS"] = ev_all.IS_FRESH.map(
        lambda v: "not recorded" if pd.isna(v) else ("fresh" if v else "stale")
    )

    # Flags hide a leg everywhere: any detection using that book on that market,
    # and that book's EV rows on it.
    keys = set(zip(flags.EVENT_ID, flags.MARKET_ID, flags.BOOKMAKER_NAME, strict=True))
    flagged = [
        k in keys
        for k in zip(
            surebet_legs.EVENT_ID, surebet_legs.MARKET_ID, surebet_legs.BOOKMAKER_NAME, strict=True
        )
    ]
    hidden = set(surebet_legs.loc[flagged, "SIGNAL_KEY"])
    surebet_legs = surebet_legs[~surebet_legs.SIGNAL_KEY.isin(hidden)]
    ev_all = ev_all[
        [
            k not in keys
            for k in zip(ev_all.EVENT_ID, ev_all.MARKET_ID, ev_all.BOOKMAKER_NAME, strict=True)
        ]
    ]
    return {
        "surebet_legs": surebet_legs,
        "track": track,
        "ev_all": ev_all,
        "signal_ticks": signal_ticks,
        "flags": flags,
    }


def _offered_text(row: pd.Series) -> str:
    if pd.isna(row.OFFERED):
        return "not checked"
    when = f" · {row.BOOK_FIRED_AT:%H:%M}" if pd.notna(row.BOOK_FIRED_AT) else ""
    if bool(row.OFFERED):
        return f"yes at {row.CURRENT_ODDS:.2f}{when}"
    return f"withdrawn{when}"


JOIN = ["EVENT_ID", "MARKET_ID", "OUTCOME_ID", "BOOKMAKER_NAME"]


register_warmer("overview-signals", _prepare)


def _data() -> dict[str, pd.DataFrame]:
    """The prepared frames, with the clock-dependent split applied fresh."""
    frames = dict(_prepare(watermark()))
    for name in ("surebet_legs", "ev_all"):
        frame = frames[name].copy()
        frame["UPCOMING"] = is_upcoming(frame.KICKOFF_AT)
        frames[name] = frame
    return frames


BET_LINK = st.column_config.LinkColumn(
    "Bet",
    display_text="open ↗",
    help="The match on this bookmaker. msport shows a first-time visitor "
    "its welcome page once; open the link again.",
)
FLAG = st.column_config.CheckboxColumn(
    "✕ not on site",
    help="Tick if the market is not on the bookmaker's site. The leg is hidden "
    "from every signal table until restored.",
    default=False,
)
EV_COLUMNS = {
    "FIXTURE": "Fixture",
    "MARKET": "Market",
    "LINE": "Line",
    "OUTCOME_NAME": "Outcome",
    "BOOKMAKER_NAME": "Book",
    "ODDS": "Odds at detection",
    "LATEST_ODDS": "Last recorded odds",
    "OFFERED_NOW": "On the book now",
    "URL": "Bet",
    "EV": "EV",
    "IMPLIED_P": "Implied p",
    "P_SOURCE": "Comparable",
    "PROBABILITY_SPREAD_SECONDS": "Time lapse (s)",
    "FRESHNESS": "Freshness",
    "KICKOFF_AT": "Kick-off",
    "DETECTED_AT": "Priced at",
    "VERDICT": "Result",
}


def _flag(event_id: str, market_id: str, book: str) -> None:
    execute(
        """
        MERGE INTO CORE.dashboard_leg_flag t
        USING (SELECT %s AS event_id, %s AS market_id, %s AS bookmaker_name) s
          ON t.event_id = s.event_id AND t.market_id = s.market_id
         AND t.bookmaker_name = s.bookmaker_name
        WHEN MATCHED THEN UPDATE SET active = TRUE, flagged_at = current_timestamp()
        WHEN NOT MATCHED THEN INSERT (event_id, market_id, bookmaker_name, active)
          VALUES (s.event_id, s.market_id, s.bookmaker_name, TRUE)
        """,
        (event_id, market_id, book),
    )


def _sizing(rows: pd.DataFrame, key: str) -> None:
    """Editable odds per leg, the arbitrage they make, and how to split a stake."""
    table = rows.reset_index(drop=True).assign(
        YOUR_ODDS=[
            float(r.CURRENT_ODDS)
            if pd.notna(r.OFFERED) and bool(r.OFFERED) and pd.notna(r.CURRENT_ODDS)
            else float(r.LATEST_ODDS)
            if pd.notna(r.LATEST_ODDS)
            else float(r.ODDS)
            for r in rows.itertuples()
        ],
        FLAG=False,
    )
    edited = st.data_editor(
        table,
        key=key,
        hide_index=True,
        use_container_width=True,
        column_order=[
            "OUTCOME",
            "BOOKMAKER_NAME",
            "ODDS",
            "OFFERED_NOW",
            "YOUR_ODDS",
            "URL",
            "FLAG",
        ],
        disabled=["OUTCOME", "BOOKMAKER_NAME", "ODDS", "OFFERED_NOW", "URL"],
        column_config={
            "OUTCOME": "Outcome",
            "BOOKMAKER_NAME": "Book",
            "ODDS": st.column_config.NumberColumn("Odds at detection", format="%.2f"),
            "OFFERED_NOW": "On the book now",
            "YOUR_ODDS": st.column_config.NumberColumn(
                "Your odds",
                min_value=1.01,
                step=0.01,
                format="%.2f",
                help="Starts at the book's current price where it was checked. "
                "Change it to what the site shows and everything below recalculates.",
            ),
            "URL": BET_LINK,
            "FLAG": FLAG,
        },
    )
    ticked = edited[edited.FLAG]
    if not ticked.empty:
        for r in ticked.itertuples():
            _flag(r.EVENT_ID, r.MARKET_ID, r.BOOKMAKER_NAME)
        st.rerun()

    split = stake_split(list(edited.YOUR_ODDS))
    if split is None:
        st.caption("Enter a price above 1.00 for every leg to size the bet.")
        return
    m1, m2 = st.columns([1, 2])
    m1.metric(
        "Arbitrage at these odds",
        f"{split.arbitrage:.4f}",
        delta=f"{split.arbitrage - 1:+.2%} on the stake",
        help="Every outcome returns stake × this. Above 1.0 is a guaranteed profit.",
    )
    stakes = edited.assign(
        SHARE=split.fractions,
        PER_100=[f * 100 for f in split.fractions],
        RETURNS=[f * 100 * o for f, o in zip(split.fractions, edited.YOUR_ODDS, strict=True)],
    )
    m2.dataframe(
        stakes[["BOOKMAKER_NAME", "OUTCOME", "SHARE", "PER_100", "RETURNS"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "BOOKMAKER_NAME": "Book",
            "OUTCOME": "Outcome",
            "SHARE": st.column_config.ProgressColumn(
                "Share of stake", min_value=0.0, max_value=1.0, format="percent"
            ),
            "PER_100": st.column_config.NumberColumn("Per 100 staked", format="%.2f"),
            "RETURNS": st.column_config.NumberColumn("Returns if it lands", format="%.2f"),
        },
    )


def _standing(track: pd.DataFrame, event_id: str, market_id: str) -> pd.Series | None:
    history = track[(track.EVENT_ID == event_id) & (track.MARKET_ID == market_id)]
    return history.iloc[-1] if not history.empty else None


def _market_label(row: pd.Series) -> str:
    line = f" {row.LINE}" if row.LINE else ""
    return f"{row.FIXTURE} — {row.MARKET_NAME}{line}"


def _upcoming_cards(legs: pd.DataFrame, track: pd.DataFrame) -> None:
    """One card per upcoming MARKET that has carried a surebet, with sizing."""
    for (event_id, market_id), rows in legs.groupby(["EVENT_ID", "MARKET_ID"], sort=False):
        head = rows.loc[rows.ARBITRAGE.idxmax()]
        newest_key = rows.loc[rows.DETECTED_AT.idxmax(), "SIGNAL_KEY"]
        newest_rows = rows[rows.SIGNAL_KEY == newest_key]
        detections = rows.drop_duplicates("SIGNAL_KEY")
        standing = _standing(track, event_id, market_id)

        with st.container(border=True):
            st.markdown(f"**{_market_label(head)}**")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric(
                "Best detected",
                f"{head.ARBITRAGE:.4f}",
                help=f"Legs {int(head.SPREAD_SECONDS)}s apart. Priced "
                f"{head.DETECTED_AT:%d %b %H:%M} (market time).",
            )
            m2.metric(
                "Detections",
                f"{len(detections)}",
                help=f"First {detections.DETECTED_AT.min():%d %b %H:%M}, "
                f"last {detections.DETECTED_AT.max():%d %b %H:%M}.",
            )
            if standing is None:
                m3.metric("Now", "—", help="Not tracked yet.")
            elif pd.isna(standing.ARBITRAGE):
                m3.metric(
                    "Now",
                    "no price",
                    help="The detector could not price this market across books "
                    "at its last check: a book withdrew it, one book was pricing every "
                    "outcome, or no probability was published for it.",
                )
            else:
                m3.metric(
                    "Now",
                    f"{standing.ARBITRAGE:.4f}",
                    delta=f"{standing.ARBITRAGE - head.ARBITRAGE:+.4f} vs best",
                )
            m4.metric("Kick-off", kickoff(head.KICKOFF_AT))
            if (
                standing is not None
                and pd.notna(standing.ARBITRAGE)
                and pd.notna(standing.LEG_SPREAD_SECONDS)
                and standing.LEG_SPREAD_SECONDS > MAX_LEG_SPREAD_SECONDS
            ):
                st.caption(
                    f"The now figure rests on prices "
                    f"{standing.LEG_SPREAD_SECONDS / 60:.0f} minutes apart: real "
                    "arithmetic, not a price anyone could take."
                )
            withdrawn = newest_rows[newest_rows.OFFERED.eq(False)]
            if not withdrawn.empty:
                st.warning(
                    "Not on the book now: "
                    + ", ".join(
                        f"{r.OUTCOME} at {r.BOOKMAKER_NAME}" for r in withdrawn.itertuples()
                    )
                    + ". The market was missing from its latest payload."
                )
            st.caption(
                "Legs of the newest detection "
                f"({newest_rows.DETECTED_AT.iloc[0]:%d %b %H:%M}). Edit **Your odds** "
                "to what the site shows; tick ✕ if the market is not there."
            )
            _sizing(newest_rows, key=f"size-{event_id}-{market_id}")

            with st.expander("Arbitrage over time", expanded=False):
                history = track[(track.EVENT_ID == event_id) & (track.MARKET_ID == market_id)]
                if history.empty:
                    st.caption(
                        "No tracked history for this market yet. It is rebuilt every "
                        "30 minutes; bronze prunes after about seven weeks."
                    )
                else:
                    st.plotly_chart(
                        arbitrage_chart(history, detections, head.KICKOFF_AT, "now"),
                        use_container_width=True,
                        key=f"arb-up-{event_id}-{market_id}",
                    )
                    st.caption(
                        f"{len(history)} recorded changes, drawn as the ~{POINTS} largest. "
                        "Red diamonds: surebet detections. Grey points: legs more than five "
                        "minutes apart."
                    )


def _arbitrage_chart_picker(legs: pd.DataFrame, track: pd.DataFrame, played: bool, key: str):
    """ONE chart for the tab, for the market picked -- not one per card."""
    markets = (
        legs.groupby(["EVENT_ID", "MARKET_ID"], sort=False)
        .agg(
            FIXTURE=("FIXTURE", "first"),
            MARKET_NAME=("MARKET_NAME", "first"),
            LINE=("LINE", "first"),
            KICKOFF_AT=("KICKOFF_AT", "first"),
            BEST=("ARBITRAGE", "max"),
        )
        .reset_index()
    )
    if markets.empty:
        return
    labels = [f"{_market_label(m)} (best {m.BEST:.4f})" for _, m in markets.iterrows()]
    picked = markets.iloc[labels.index(st.selectbox("Arbitrage over time for", labels, key=key))]
    history = track[(track.EVENT_ID == picked.EVENT_ID) & (track.MARKET_ID == picked.MARKET_ID)]
    if history.empty:
        st.caption(
            "No tracked history for this market yet. It is rebuilt every 30 "
            "minutes; bronze prunes after about seven weeks."
        )
        return
    detections = legs[
        (legs.EVENT_ID == picked.EVENT_ID) & (legs.MARKET_ID == picked.MARKET_ID)
    ].drop_duplicates("SIGNAL_KEY")
    st.plotly_chart(
        arbitrage_chart(history, detections, picked.KICKOFF_AT, "at kick-off" if played else "now"),
        use_container_width=True,
        key=f"{key}-chart",
    )
    st.caption(
        f"{len(history)} recorded changes, drawn as the ~{POINTS} largest. Red "
        "diamonds: surebet detections. Grey points: legs more than five minutes apart."
    )


def _ev_chart(rows: pd.DataFrame, ticks: pd.DataFrame, ev_min: float, played: bool, key: str):
    groups = (
        rows.groupby(JOIN, as_index=False)
        .agg(
            FIXTURE=("FIXTURE", "first"),
            MARKET=("MARKET", "first"),
            LINE=("LINE", "first"),
            OUTCOME_NAME=("OUTCOME_NAME", "first"),
            KICKOFF_AT=("KICKOFF_AT", "first"),
            BEST=("EV", "max"),
        )
        .sort_values("BEST", ascending=False)
    )
    if groups.empty:
        return
    labels = [
        f"{g.FIXTURE} — {g.MARKET}{' ' + g.LINE if g.LINE else ''} · "
        f"{g.OUTCOME_NAME} @ {g.BOOKMAKER_NAME} (best EV {g.BEST:.3f})"
        for g in groups.itertuples()
    ]
    g = groups.iloc[labels.index(st.selectbox("Chart an opportunity", labels, key=f"{key}-pick"))]
    prices = ticks[
        (ticks.EVENT_ID == g.EVENT_ID)
        & (ticks.MARKET_ID == g.MARKET_ID)
        & (ticks.OUTCOME_ID == g.OUTCOME_ID)
    ]
    x_end = (
        pd.to_datetime(g.KICKOFF_AT, utc=True)
        if played and pd.notna(g.KICKOFF_AT)
        else pd.Timestamp.now(tz="UTC")
    )
    prices = prices[pd.to_datetime(prices.FIRE_TIME, utc=True) <= x_end]
    marks = rows[
        (rows.EVENT_ID == g.EVENT_ID)
        & (rows.MARKET_ID == g.MARKET_ID)
        & (rows.OUTCOME_ID == g.OUTCOME_ID)
    ][["DETECTED_AT", "ODDS", "EV", "BOOKMAKER_NAME"]]
    if prices.empty:
        st.caption("No price history extracted for this outcome yet.")
        return
    st.plotly_chart(
        odds_chart(prices, g.BOOKMAKER_NAME, marks, g.KICKOFF_AT, f"EV ≥ {ev_min:.3f}", x_end),
        use_container_width=True,
        key=f"{key}-chart",
    )
    st.caption(
        f"Every book's price for {g.OUTCOME_NAME} in its site's colour, each line its "
        f"~{POINTS} largest moves and held until changed; {g.BOOKMAKER_NAME} in bold. "
        "Diamonds: EV at or above the threshold. Star: the last recorded price, "
        f"{'at kick-off' if played else 'now'}."
    )


def _hidden_flags(flags: pd.DataFrame) -> None:
    if flags.empty:
        return
    with st.popover(f"Hidden by flags ({len(flags)})"):
        for r in flags.itertuples():
            left, right = st.columns([4, 1])
            left.caption(
                f"{r.BOOKMAKER_NAME} · market {r.MARKET_ID} · event "
                f"{str(r.EVENT_ID)[:8]} · flagged {r.FLAGGED_AT:%d %b %H:%M}"
            )
            if right.button(
                "Restore", key=f"restore-{r.EVENT_ID}-{r.MARKET_ID}-{r.BOOKMAKER_NAME}"
            ):
                execute(
                    "UPDATE CORE.dashboard_leg_flag SET active = FALSE "
                    "WHERE event_id = %s AND market_id = %s AND bookmaker_name = %s",
                    (r.EVENT_ID, r.MARKET_ID, r.BOOKMAKER_NAME),
                )
                st.rerun()


# --- section 2: arbitrage, upcoming and past --------------------------------------
# Fragments: a tab, a picker or an edited price reruns only its own section,
# never the whole page. Navigation between pages still reruns the page -- that
# is how Streamlit works -- but everything it reads is cached.


@st.fragment
def arbitrage_section() -> None:
    data = _data()
    legs, track = data["surebet_legs"], data["track"]
    upcoming = legs[legs.UPCOMING & legs.PRE_MATCH.eq(True)]
    past = legs[~legs.UPCOMING]
    _hidden_flags(data["flags"])
    up_tab, past_tab = st.tabs(
        [
            f"Upcoming ({len(upcoming.drop_duplicates(['EVENT_ID', 'MARKET_ID']))})",
            f"Past ({len(past.drop_duplicates(['EVENT_ID', 'MARKET_ID']))})",
        ]
    )
    with up_tab:
        st.caption(
            "Markets on fixtures that have NOT kicked off where a surebet has been "
            "detected. Each card says where it stands now, including below 1.0, "
            "whether every leg is still on the book, and how to split a stake at the "
            "odds you enter. Every leg of a detection was priced within five minutes "
            "of the others; wider gaps are refused."
        )
        if upcoming.empty:
            st.info(
                "No surebet has been detected on a fixture still to be played. That "
                "is the normal state: real surebets across public books are rare and "
                "close within minutes."
            )
        else:
            _upcoming_cards(upcoming, track)

    with past_tab:
        _efficiency()
        st.markdown("**Past surebets**")
        stale = query(
            """
            SELECT count(*) AS n, max(arbitrage) AS worst
            FROM ANALYTICS.stg_arbitrage_signal
            WHERE arbitrage > 1 AND NOT is_fresh
            """
        ).iloc[0]
        if int(stale.N):
            st.caption(
                f"{int(stale.N)} further signals above 1.0 are not shown anywhere, the "
                f"largest {stale.WORST:.4f}: legs priced more than five minutes apart, "
                "never on sale together. Kept as the evidence for the freshness rule."
            )
        if past.empty:
            st.caption("None.")
            return
        summary = (
            past.groupby(["EVENT_ID", "MARKET_ID"], sort=False)
            .agg(
                FIXTURE=("FIXTURE", "first"),
                MARKET_NAME=("MARKET_NAME", "first"),
                LINE=("LINE", "first"),
                KICKOFF_AT=("KICKOFF_AT", "first"),
                BEST=("ARBITRAGE", "max"),
                DETECTIONS=("SIGNAL_KEY", "nunique"),
                IN_PLAY=("PRE_MATCH", lambda v: bool((~v.astype(bool)).any())),
            )
            .reset_index()
        )
        summary["AT_KICKOFF"] = [
            None if (s := _standing(track, e, m)) is None or pd.isna(s.ARBITRAGE) else s.ARBITRAGE
            for e, m in zip(summary.EVENT_ID, summary.MARKET_ID, strict=True)
        ]
        summary["MARKET"] = summary.MARKET_NAME + summary.LINE.map(
            lambda v: f" {v}" if pd.notna(v) and v else ""
        )
        st.dataframe(
            summary.sort_values("KICKOFF_AT", ascending=False)[
                ["FIXTURE", "MARKET", "KICKOFF_AT", "BEST", "DETECTIONS", "AT_KICKOFF", "IN_PLAY"]
            ],
            hide_index=True,
            use_container_width=True,
            column_config={
                "FIXTURE": "Fixture",
                "MARKET": "Market",
                "KICKOFF_AT": st.column_config.DatetimeColumn("Kick-off", format="D MMM HH:mm"),
                "BEST": st.column_config.NumberColumn("Best detected", format="%.4f"),
                "DETECTIONS": "Detections",
                "AT_KICKOFF": st.column_config.NumberColumn(
                    "At kick-off",
                    format="%.4f",
                    help="Where the detector's replay stood at kick-off. Blank: no "
                    "cross-book price by then.",
                ),
                "IN_PLAY": st.column_config.CheckboxColumn(
                    "In-play detection", help="Priced after kick-off: never a pre-match bet."
                ),
            },
        )
        _arbitrage_chart_picker(past, track, played=True, key="arb-past")


def _efficiency() -> None:
    st.markdown("**Market efficiency**")
    st.caption(
        "Overround left after shopping every book: `1 / arbitrage - 1`, averaged over "
        "every signal recorded on the market. Zero means the best prices exactly "
        "cancel; **red, below zero, is the surebet side**; blue is the margin a "
        "punter still pays at the best prices. Near misses are used here on "
        "purpose. Not per-bookmaker vig."
    )
    by_line = st.toggle("Split each market by line", value=False, key="efficiency-by-line")
    # Each grain is its own query: Snowflake does the grouping, and each result
    # is cached on its own, so flipping the switch back is instant. One bar per
    # market (the default) or per market and line -- never per WEEK, which is
    # what made the first chart's bars stack.
    grain = "s.specifier" if by_line else "NULL"
    efficiency = query(
        f"""
        SELECT COALESCE(m.market_name, 'market ' || s.market_base_id)
                 || COALESCE(' ' || {grain}, '')                AS label,
               count(*)                                          AS signals,
               avg(s.overround)                                  AS mean_overround
        FROM ANALYTICS.stg_arbitrage_signal s
        LEFT JOIN CORE.dim_market m ON m.market_base_id = s.market_base_id
        GROUP BY 1
        """
    )
    if efficiency.empty:
        st.info("No signals yet.")
    else:
        st.plotly_chart(efficiency_bars(efficiency), use_container_width=True)


# --- section 3: positive EV, upcoming and past ------------------------------------


@st.cache_data(show_spinner=False)
def _backtest(mark: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    del mark
    settled = query(
        """
        SELECT signal_key, event_id, market_id, outcome_id, fixture, tournament,
               market_name, line, outcome_name, bookmaker_name, odds, implied_p, ev,
               is_fresh, detected_at, kickoff_at, verdict
        FROM ANALYTICS.gold_ev_settled
        """
    )
    settled["OUTCOME_ID"] = settled.OUTCOME_ID.astype(str)
    return settled, compare(settled)


register_warmer("overview-backtest", _backtest)

SIZING_LABELS = {
    "flat": "Flat 1% of start",
    "fixed_2pct": "2% of bankroll",
    "kelly": "Full Kelly",
    "half_kelly": "Half Kelly",
    "quarter_kelly": "Quarter Kelly",
}


def _backtest_panel() -> None:
    settled, table = _backtest(watermark())
    n_settled = settled.dropna(subset=["VERDICT"]).drop_duplicates(
        ["EVENT_ID", "MARKET_ID", "OUTCOME_ID"]
    )
    st.markdown("**Backtest: what if every past positive EV had been bet?**")
    st.caption(
        f"{len(n_settled)} settled opportunities (fixture, market, outcome) from "
        f"{settled.KICKOFF_AT.min():%d %b} to {settled.KICKOFF_AT.max():%d %b}. Starting "
        "bankroll 100. Stakes come from cash not tied up in unsettled bets; each bet "
        "settles two hours after kick-off. Kelly stakes `EV / (odds − 1)` of the "
        "bankroll, which is growth-optimal only if the probability is right — here it "
        "is a bookmaker's, margin included."
    )
    if n_settled.empty:
        st.info("No positive-EV opportunity has settled yet.")
        return
    timing = (
        st.segmented_control(
            "Take the price",
            options=list(TIMINGS),
            format_func={
                "first": "when first seen",
                "best": "at its best EV",
                "last": "last before kick-off",
            }.get,
            default="first",
            key="backtest-timing",
        )
        or "first"
    )
    chosen = choose(settled, timing)
    curves = []
    for sizing in SIZINGS:
        result = simulate(chosen, sizing)
        if not result.curve.empty:
            curves.append(result.curve.assign(STRATEGY=SIZING_LABELS[sizing]))
    if curves:
        figure = px.line(
            pd.concat(curves),
            x="AT",
            y="BANKROLL",
            color="STRATEGY",
            line_shape="hv",
            labels={"AT": "", "BANKROLL": "bankroll", "STRATEGY": ""},
        )
        figure.add_hline(y=100, line_dash="dot", line_color="#888")
        figure.update_layout(
            height=340,
            margin={"t": 10, "b": 0, "l": 0, "r": 0},
            legend={"orientation": "h", "y": -0.2},
        )
        st.plotly_chart(figure, use_container_width=True, key="backtest-chart")

    ranked = table.assign(
        STRATEGY=table.sizing.map(SIZING_LABELS),
        TIMING=table.timing.map(
            {"first": "first seen", "best": "best EV", "last": "last before kick-off"}
        ),
        # Growth per unit of pain: final gain over the worst fall from a peak.
        SCORE=(table.final - 100) / table.max_drawdown.clip(lower=0.01) / 100,
    ).sort_values("SCORE", ascending=False)
    best = ranked.iloc[0]
    richest = ranked.sort_values("final", ascending=False).iloc[0]
    st.success(
        f"**Best risk-adjusted: {best.STRATEGY}, taking the price {best.TIMING}** — "
        f"bankroll 100 → {best.final:.0f}, worst drawdown {best.max_drawdown:.0%}. "
        f"Highest final bankroll: {richest.STRATEGY} ({richest.TIMING}), 100 → "
        f"{richest.final:.0f} with a {richest.max_drawdown:.0%} drawdown. "
        f"On {len(n_settled)} opportunities this is a reading, not a law."
    )
    st.dataframe(
        ranked[
            ["STRATEGY", "TIMING", "bets", "hit_rate", "staked", "roi", "final", "max_drawdown"]
        ],
        hide_index=True,
        use_container_width=True,
        column_config={
            "STRATEGY": "Sizing",
            "TIMING": "Timing",
            "bets": "Bets",
            "hit_rate": st.column_config.NumberColumn("Hit rate", format="percent"),
            "staked": st.column_config.NumberColumn("Staked", format="%.1f"),
            "roi": st.column_config.NumberColumn("ROI on stakes", format="percent"),
            "final": st.column_config.NumberColumn("Final bankroll", format="%.1f"),
            "max_drawdown": st.column_config.NumberColumn("Max drawdown", format="percent"),
        },
    )


@st.fragment
def ev_section() -> None:
    data = _data()
    ev_all, ticks = data["ev_all"], data["signal_ticks"]
    ev_min = st.slider(
        "Show EV opportunities from",
        min_value=0.0,
        max_value=0.10,
        value=0.015,
        step=0.005,
        format="%.3f",
        help="Expected value per unit staked. 0.015 means the price beats its probability "
        "by 1.5% of the stake. Applies to the tables and charts below; the backtest "
        "uses every positive EV.",
    )
    shown = ev_all[ev_all.EV >= ev_min]
    upcoming = shown[shown.UPCOMING & shown.IS_FRESH.eq(True)]
    past = shown[~shown.UPCOMING]
    up_tab, past_tab = st.tabs([f"Upcoming ({len(upcoming)})", f"Past ({len(past)})"])

    with up_tab:
        st.caption(
            f"Fixtures that have NOT kicked off: every outcome whose price beat its "
            f"probability by at least {ev_min:.3f}. The probability is the most recent one "
            "sportybet or msport published, within five minutes of the price. **On the "
            "book now** is its latest payload, checked every 30 minutes."
        )
        if upcoming.empty:
            st.info(f"No EV of {ev_min:.3f} or more on a fixture still to be played.")
        else:
            columns = [c for c in EV_COLUMNS if c in upcoming.columns and c != "VERDICT"]
            edited = st.data_editor(
                upcoming.reset_index(drop=True).assign(FLAG=False),
                key="ev-up-editor",
                hide_index=True,
                use_container_width=True,
                column_order=[*columns, "FLAG"],
                disabled=columns,
                column_config={
                    **{c: EV_COLUMNS[c] for c in columns},
                    "URL": BET_LINK,
                    "FLAG": FLAG,
                },
            )
            ticked = edited[edited.FLAG]
            if not ticked.empty:
                for r in ticked.itertuples():
                    _flag(r.EVENT_ID, r.MARKET_ID, r.BOOKMAKER_NAME)
                st.rerun()
            _ev_chart(upcoming, ticks, ev_min, played=False, key="ev-up")

    with past_tab:
        st.markdown("**Positive EV by book**")
        st.caption(
            "Only sportybet and msport publish a probability alongside their prices, so "
            "every other book's EV is measured against a borrowed one."
        )
        by_book = query(
            """
            SELECT bookmaker_name, count(*) AS signals, avg(ev) AS mean_ev
            FROM ANALYTICS.stg_ev_signal
            GROUP BY 1 ORDER BY 2 DESC
            """
        )
        if not by_book.empty:
            st.plotly_chart(
                book_bars(by_book, "SIGNALS", "EV signals"),
                use_container_width=True,
                key="ev-by-book",
            )
        _backtest_panel()
        st.markdown(f"**Past opportunities** · {len(past)} at {ev_min:.3f} or more")
        if past.empty:
            st.caption("None.")
        else:
            settled, _ = _backtest(watermark())
            with_result = past.merge(
                settled[
                    [
                        "EVENT_ID",
                        "MARKET_ID",
                        "OUTCOME_ID",
                        "BOOKMAKER_NAME",
                        "DETECTED_AT",
                        "VERDICT",
                    ]
                ],
                on=["EVENT_ID", "MARKET_ID", "OUTCOME_ID", "BOOKMAKER_NAME", "DETECTED_AT"],
                how="left",
            )
            columns = [c for c in EV_COLUMNS if c in with_result.columns]
            st.dataframe(
                with_result[columns].rename(columns=EV_COLUMNS),
                column_config={"Bet": BET_LINK},
                hide_index=True,
                use_container_width=True,
            )
            _ev_chart(past, ticks, ev_min, played=True, key="ev-past")


with st.expander("Arbitrage", expanded=True):
    arbitrage_section()

with st.expander("Positive EV", expanded=True):
    ev_section()

st.divider()
st.caption(
    "**What these numbers are not.** Every source behind this page rolls — bronze "
    "prunes, the fixture window moves — so these figures are a reading taken at "
    "the time given above, not a standing claim. Reference implementation, not a "
    "betting service."
)
