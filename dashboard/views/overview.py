"""Cross-bookmaker market signals: the dashboard's front page.

Everything here comes from the 'signals' document the runner publishes to
Supabase (dashboard/common.py): headline numbers, every surebet leg, where each
surebet market's arbitrage has stood since, every positive-EV detection with
its price history, and the precomputed backtest. The hot loop republishes the
document within about 30 seconds of a new signal.

Every section below the headline numbers is collapsible. Arbitrage and positive
EV each have an Upcoming and a Past tab: what can still be bet, and the record.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st
from dashboard.arbitrage import stake_split
from dashboard.charts import arbitrage_chart, book_bars, efficiency_bars, odds_chart
from dashboard.common import (
    TIMEZONE,
    document,
    flags,
    frame,
    is_upcoming,
    kickoff,
    points,
    published_at,
    set_flag,
    versions,
)
from dashboard.series import MAX_LEG_SPREAD_SECONDS, POINTS

doc = document("signals")

st.title("Arbibet — cross-bookmaker market signals")
st.caption(
    "Five Nigerian and international bookmakers, reconciled onto one market "
    "taxonomy. Every number below is measured, and the ones that mean less "
    "than they look are labelled."
)
asof = doc["asof"]
st.caption(
    f"Newest signal {published_at(asof['lastSignal'])} · "
    f"newest price {published_at(asof['lastTick'])} · "
    f"newest slip verdict {published_at(asof['lastSummary'])} · "
    f"published {published_at(versions().get('signals'))}"
)
st.caption(
    "Signals are recomputed within seconds of a bookmaker publishing a new price, "
    "and this page picks them up within a minute."
)

# --- headline numbers ------------------------------------------------------
counts = doc["counts"]
a, b, c, d, e = st.columns(5)
a.metric(
    "True surebets",
    f"{counts['surebets']:,}",
    help=(
        "arbitrage > 1, priced before kick-off, every leg within five minutes "
        "of the others. All time — the ones still placeable are under "
        "Upcoming below. Genuine surebets across public books barely exist."
    ),
)
b.metric("Positive-EV", f"{counts['evSignals']:,}", help="Priced before kick-off. All time.")
c.metric("Price changes", f"{counts['ticks']:,}", help="Every published price move, replayed.")
d.metric(
    "Markets settled", f"{counts['settled']:,}", help="Historical results, per team per market."
)
e.metric("Slips analysed", f"{counts['slips']:,}")

# --- how an arbitrage is calculated ----------------------------------------------
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
    example = doc.get("example")
    if example:
        legs = pd.DataFrame(example["legs"])
        split = stake_split(list(legs.odds))
        line = f" {example['line']}" if example.get("line") else ""
        st.markdown(
            f"**Worked example — the newest surebet:** {example['fixture']}, "
            f"{example['market']}{line}, priced {kickoff(example['detectedAt'])}, "
            f"legs {int(example['spreadSeconds'])}s apart."
        )
        worked = legs.assign(inverse=1 / legs.odds, stake=split.fractions if split else None)
        st.dataframe(
            worked.rename(
                columns={
                    "outcome": "Outcome",
                    "book": "Best book",
                    "odds": "Odds",
                    "inverse": "1 / odds",
                    "stake": "Stake share",
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
        total = worked.inverse.sum()
        st.caption(
            f"Σ 1/odds = {total:.4f}, so arbitrage = 1 / {total:.4f} = **{1 / total:.4f}**: "
            f"{(1 / total - 1):+.2%} on the total stake, whatever wins."
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
    freshness = pd.DataFrame(doc["freshness"])
    if not freshness.empty:
        left, right = st.columns([2, 3])
        left.dataframe(
            freshness.rename(
                columns={"spread": "Legs apart", "signals": "Signals", "surebets": "Surebets"}
            ),
            hide_index=True,
            use_container_width=True,
        )
        right.bar_chart(freshness.set_index("spread")[["signals"]], height=240)

# --- data, prepared once per published document -------------------------------------

JOIN = ["EVENT_ID", "MARKET_ID", "OUTCOME_ID", "BOOKMAKER_NAME"]
_AVAILABILITY = {
    "latestOdds": "LATEST_ODDS",
    "offered": "OFFERED",
    "currentOdds": "CURRENT_ODDS",
    "bookFiredAt": "BOOK_FIRED_AT",
}


@st.cache_data(show_spinner=False, max_entries=4)
def _prepare(version: str) -> dict[str, pd.DataFrame]:
    del version
    body = document("signals")
    arb, ev = body["arbitrage"], body["ev"]
    legs = frame(
        arb["legs"],
        {
            "signalKey": "SIGNAL_KEY",
            "eventId": "EVENT_ID",
            "marketId": "MARKET_ID",
            "outcomeId": "OUTCOME_ID",
            "fixture": "FIXTURE",
            "market": "MARKET_NAME",
            "line": "LINE",
            "outcome": "OUTCOME",
            "book": "BOOKMAKER_NAME",
            "odds": "ODDS",
            "url": "URL",
            "arbitrage": "ARBITRAGE",
            "spreadSeconds": "SPREAD_SECONDS",
            "detectedAt": "DETECTED_AT",
            "kickoffAt": "KICKOFF_AT",
            "preMatch": "PRE_MATCH",
            **_AVAILABILITY,
        },
        times=("DETECTED_AT", "KICKOFF_AT", "BOOK_FIRED_AT"),
    )
    tracks = []
    for key, track in arb["tracks"].items():
        event_id, market_id = key.split("|", 1)
        part = points(track["points"], "OBSERVED_AT", "ARBITRAGE", "LEG_SPREAD_SECONDS")
        tracks.append(part.assign(EVENT_ID=event_id, MARKET_ID=market_id, TOTAL=track["total"]))
    track = (
        pd.concat(tracks, ignore_index=True)
        if tracks
        else pd.DataFrame(
            columns=[
                "OBSERVED_AT",
                "ARBITRAGE",
                "LEG_SPREAD_SECONDS",
                "EVENT_ID",
                "MARKET_ID",
                "TOTAL",
            ]
        )
    )
    standing = pd.DataFrame(
        [
            {
                "EVENT_ID": key.split("|", 1)[0],
                "MARKET_ID": key.split("|", 1)[1],
                "ARBITRAGE": t["last"]["arbitrage"],
                "LEG_SPREAD_SECONDS": t["last"]["spreadSeconds"],
            }
            for key, t in arb["tracks"].items()
        ],
        columns=["EVENT_ID", "MARKET_ID", "ARBITRAGE", "LEG_SPREAD_SECONDS"],
    )
    ev_all = frame(
        ev["rows"],
        {
            "eventId": "EVENT_ID",
            "marketId": "MARKET_ID",
            "outcomeId": "OUTCOME_ID",
            "fixture": "FIXTURE",
            "market": "MARKET",
            "line": "LINE",
            "outcome": "OUTCOME_NAME",
            "book": "BOOKMAKER_NAME",
            "odds": "ODDS",
            "url": "URL",
            "impliedP": "IMPLIED_P",
            "comparable": "P_SOURCE",
            "timeLapseSeconds": "PROBABILITY_SPREAD_SECONDS",
            "ev": "EV",
            "kickoffAt": "KICKOFF_AT",
            "detectedAt": "DETECTED_AT",
            "isFresh": "IS_FRESH",
            "verdict": "VERDICT",
            **_AVAILABILITY,
        },
        times=("DETECTED_AT", "KICKOFF_AT", "BOOK_FIRED_AT"),
    )
    ticks = []
    for key, books in ev["prices"].items():
        event_id, market_id, outcome_id = key.split("|", 2)
        for book, series in books.items():
            part = points(series, "FIRE_TIME", "ODDS")
            ticks.append(
                part.assign(
                    EVENT_ID=event_id,
                    MARKET_ID=market_id,
                    OUTCOME_ID=outcome_id,
                    BOOKMAKER_NAME=book,
                )
            )
    signal_ticks = (
        pd.concat(ticks, ignore_index=True)
        if ticks
        else pd.DataFrame(columns=["FIRE_TIME", "ODDS", *JOIN])
    )
    for data in (legs, ev_all):
        data["OFFERED_NOW"] = data.apply(_offered_text, axis=1) if not data.empty else []
    ev_all["FRESHNESS"] = ev_all.IS_FRESH.map(
        lambda v: "not recorded" if pd.isna(v) else ("fresh" if v else "stale")
    )
    return {
        "surebet_legs": legs,
        "track": track,
        "standing": standing,
        "ev_all": ev_all,
        "signal_ticks": signal_ticks,
    }


def _offered_text(row: pd.Series) -> str:
    if pd.isna(row.OFFERED):
        return "not checked"
    when = f" · {row.BOOK_FIRED_AT:%H:%M}" if pd.notna(row.BOOK_FIRED_AT) else ""
    if bool(row.OFFERED):
        return f"yes at {row.CURRENT_ODDS:.2f}{when}"
    return f"withdrawn{when}"


def _data() -> dict[str, pd.DataFrame]:
    """The prepared frames, with viewer flags and the clock applied fresh."""
    frames = dict(_prepare(versions().get("signals", "")))
    active = flags()
    keys = set(zip(active.EVENT_ID, active.MARKET_ID, active.BOOKMAKER_NAME, strict=True))
    legs = frames["surebet_legs"]
    flagged = [
        k in keys for k in zip(legs.EVENT_ID, legs.MARKET_ID, legs.BOOKMAKER_NAME, strict=True)
    ]
    hidden = set(legs.loc[flagged, "SIGNAL_KEY"])
    legs = legs[~legs.SIGNAL_KEY.isin(hidden)].copy()
    ev_all = frames["ev_all"]
    ev_all = ev_all[
        [
            k not in keys
            for k in zip(ev_all.EVENT_ID, ev_all.MARKET_ID, ev_all.BOOKMAKER_NAME, strict=True)
        ]
    ].copy()
    legs["UPCOMING"] = is_upcoming(legs.KICKOFF_AT)
    ev_all["UPCOMING"] = is_upcoming(ev_all.KICKOFF_AT)
    return {**frames, "surebet_legs": legs, "ev_all": ev_all, "flags": active}


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
            set_flag(r.EVENT_ID, r.MARKET_ID, r.BOOKMAKER_NAME, True)
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


def _standing(standing: pd.DataFrame, event_id: str, market_id: str) -> pd.Series | None:
    row = standing[(standing.EVENT_ID == event_id) & (standing.MARKET_ID == market_id)]
    return row.iloc[0] if not row.empty else None


def _market_label(row: pd.Series) -> str:
    line = f" {row.LINE}" if isinstance(row.LINE, str) and row.LINE else ""
    return f"{row.FIXTURE} — {row.MARKET_NAME}{line}"


def _track_chart(
    track: pd.DataFrame, detections: pd.DataFrame, kickoff_at: object, now_label: str, key: str
) -> None:
    if track.empty:
        st.caption("No tracked history for this market yet. It is rebuilt every warm cycle.")
        return
    st.plotly_chart(
        arbitrage_chart(track, detections, kickoff_at, now_label),
        use_container_width=True,
        key=key,
    )
    st.caption(
        f"{int(track.TOTAL.iloc[0])} recorded changes, drawn as the ~{POINTS} largest. "
        "Red diamonds: surebet detections. Grey points: legs more than five minutes apart."
    )


def _upcoming_cards(legs: pd.DataFrame, track: pd.DataFrame, standing_all: pd.DataFrame) -> None:
    """One card per upcoming MARKET that has carried a surebet, with sizing."""
    for (event_id, market_id), rows in legs.groupby(["EVENT_ID", "MARKET_ID"], sort=False):
        head = rows.loc[rows.ARBITRAGE.idxmax()]
        newest_key = rows.loc[rows.DETECTED_AT.idxmax(), "SIGNAL_KEY"]
        newest_rows = rows[rows.SIGNAL_KEY == newest_key]
        detections = rows.drop_duplicates("SIGNAL_KEY")
        standing = _standing(standing_all, event_id, market_id)

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
                _track_chart(
                    history, detections, head.KICKOFF_AT, "now", f"arb-up-{event_id}-{market_id}"
                )


def _arbitrage_chart_picker(
    legs: pd.DataFrame, track: pd.DataFrame, played: bool, key: str
) -> None:
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
    detections = legs[
        (legs.EVENT_ID == picked.EVENT_ID) & (legs.MARKET_ID == picked.MARKET_ID)
    ].drop_duplicates("SIGNAL_KEY")
    _track_chart(
        history, detections, picked.KICKOFF_AT, "at kick-off" if played else "now", f"{key}-chart"
    )


def _ev_chart(
    rows: pd.DataFrame, ticks: pd.DataFrame, ev_min: float, played: bool, key: str
) -> None:
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
        f"{g.FIXTURE} — {g.MARKET}{' ' + g.LINE if isinstance(g.LINE, str) and g.LINE else ''} · "
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


def _hidden_flags(active: pd.DataFrame) -> None:
    if active.empty:
        return
    with st.popover(f"Hidden by flags ({len(active)})"):
        for r in active.itertuples():
            left, right = st.columns([4, 1])
            left.caption(
                f"{r.BOOKMAKER_NAME} · market {r.MARKET_ID} · event "
                f"{str(r.EVENT_ID)[:8]} · flagged {r.FLAGGED_AT:%d %b %H:%M}"
            )
            if right.button(
                "Restore", key=f"restore-{r.EVENT_ID}-{r.MARKET_ID}-{r.BOOKMAKER_NAME}"
            ):
                set_flag(r.EVENT_ID, r.MARKET_ID, r.BOOKMAKER_NAME, False)
                st.rerun()


# --- arbitrage, upcoming and past --------------------------------------------------


@st.fragment
def _excluded_books() -> None:
    rows = doc["arbitrage"].get("excluded") or []
    if not rows:
        return
    with st.expander(f"Books excluded from fixtures ({len(rows)})", expanded=False):
        st.caption(
            "The matcher upstream sometimes files two matches under one fixture. Every "
            "book's payload is checked against the fixture's teams before its prices are "
            "compared -- by name, and by gpt-4o-mini when names only partly agree -- and a "
            "book found on a different match is excluded from that fixture, its signals "
            "removed. This is that record."
        )
        table = pd.DataFrame(rows)
        table["kickoffAt"] = pd.to_datetime(table.kickoffAt, utc=True).dt.tz_convert(TIMEZONE)
        table["checkedAt"] = pd.to_datetime(table.checkedAt, utc=True).dt.tz_convert(TIMEZONE)
        table["listed"] = table.bookHome.fillna("?") + " v " + table.bookAway.fillna("?")
        st.dataframe(
            table[["fixture", "kickoffAt", "book", "listed", "method", "explanation", "checkedAt"]],
            hide_index=True,
            use_container_width=True,
            column_config={
                "fixture": "Fixture",
                "kickoffAt": st.column_config.DatetimeColumn("Kick-off", format="D MMM HH:mm"),
                "book": "Book",
                "listed": "The book lists",
                "method": "Found by",
                "explanation": "Why",
                "checkedAt": st.column_config.DatetimeColumn("Checked", format="D MMM HH:mm"),
            },
        )


def arbitrage_section() -> None:
    data = _data()
    _excluded_books()
    legs, track, standing = data["surebet_legs"], data["track"], data["standing"]
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
            _upcoming_cards(upcoming, track, standing)

    with past_tab:
        _efficiency()
        st.markdown("**Past surebets**")
        stale = doc["arbitrage"]["stale"]
        if stale["n"]:
            st.caption(
                f"{int(stale['n'])} further signals above 1.0 are not shown anywhere, the "
                f"largest {stale['worst']:.4f}: legs priced more than five minutes apart, "
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
            None
            if (s := _standing(standing, e, m)) is None or pd.isna(s.ARBITRAGE)
            else s.ARBITRAGE
            for e, m in zip(summary.EVENT_ID, summary.MARKET_ID, strict=True)
        ]
        summary["MARKET"] = summary.MARKET_NAME + summary.LINE.map(
            lambda v: f" {v}" if isinstance(v, str) and v else ""
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
    grain = "byLine" if by_line else "byMarket"
    efficiency = pd.DataFrame(doc["arbitrage"]["efficiency"][grain]).rename(
        columns={"label": "LABEL", "signals": "SIGNALS", "meanOverround": "MEAN_OVERROUND"}
    )
    if efficiency.empty:
        st.info("No signals yet.")
    else:
        st.plotly_chart(efficiency_bars(efficiency), use_container_width=True)


# --- positive EV, upcoming and past ----------------------------------------------

SIZING_LABELS = {
    "flat": "Flat 1% of start",
    "fixed_2pct": "2% of bankroll",
    "kelly": "Full Kelly",
    "half_kelly": "Half Kelly",
    "quarter_kelly": "Quarter Kelly",
}
TIMING_LABELS = {"first": "first seen", "best": "best EV", "last": "last before kick-off"}


def _backtest_panel() -> None:
    backtest = doc["ev"]["backtest"]
    st.markdown("**Backtest: what if every past positive EV had been bet?**")
    if not backtest["opportunities"]:
        st.info("No positive-EV opportunity has settled yet.")
        return
    st.caption(
        f"{backtest['opportunities']} settled opportunities (fixture, market, outcome) from "
        f"{kickoff(backtest['from'])[:6]} to {kickoff(backtest['to'])[:6]}. Starting "
        "bankroll 100. Stakes come from cash not tied up in unsettled bets; each bet "
        "settles two hours after kick-off. Kelly stakes `EV / (odds − 1)` of the "
        "bankroll, which is growth-optimal only if the probability is right — here it "
        "is a bookmaker's, margin included."
    )
    timing = (
        st.segmented_control(
            "Take the price",
            options=list(TIMING_LABELS),
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
    curves = [
        points(series, "AT", "BANKROLL").assign(STRATEGY=SIZING_LABELS[sizing])
        for sizing, series in backtest["curves"].get(timing, {}).items()
        if series
    ]
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

    table = pd.DataFrame(backtest["table"])
    ranked = table.assign(
        STRATEGY=table.sizing.map(SIZING_LABELS),
        TIMING=table.timing.map(TIMING_LABELS),
        SCORE=(table.final - 100) / table.max_drawdown.clip(lower=0.01) / 100,
    ).sort_values("SCORE", ascending=False)
    best = ranked.iloc[0]
    richest = ranked.sort_values("final", ascending=False).iloc[0]
    st.success(
        f"**Best risk-adjusted: {best.STRATEGY}, taking the price {best.TIMING}** — "
        f"bankroll 100 → {best.final:.0f}, worst drawdown {best.max_drawdown:.0%}. "
        f"Highest final bankroll: {richest.STRATEGY} ({richest.TIMING}), 100 → "
        f"{richest.final:.0f} with a {richest.max_drawdown:.0%} drawdown. "
        f"On {backtest['opportunities']} opportunities this is a reading, not a law."
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
            "book now** is its latest payload, checked every warm cycle."
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
                    set_flag(r.EVENT_ID, r.MARKET_ID, r.BOOKMAKER_NAME, True)
                st.rerun()
            _ev_chart(upcoming, ticks, ev_min, played=False, key="ev-up")

    with past_tab:
        st.markdown("**Positive EV by book**")
        st.caption(
            "Only sportybet and msport publish a probability alongside their prices, so "
            "every other book's EV is measured against a borrowed one."
        )
        by_book = pd.DataFrame(doc["ev"]["byBook"]).rename(
            columns={"book": "BOOKMAKER_NAME", "signals": "SIGNALS", "meanEv": "MEAN_EV"}
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
            columns = [c for c in EV_COLUMNS if c in past.columns]
            st.dataframe(
                past[columns].rename(columns=EV_COLUMNS),
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
