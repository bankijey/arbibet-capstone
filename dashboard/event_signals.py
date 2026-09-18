"""One fixture's signals, for its event page: arbitrage history and EV history.

The fixture page (views/fixture.py) is the single place a fixture's story is
told -- every Telegram "history" button and every link from the signal tables
lands there -- so the surebets and EV found on a fixture are shown beside its
brief, prices, form, settled markets and slip analysis.

Data: the 'signals' document, filtered to one event. Nothing here queries
anything; the pure part (`for_event`) is tested.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st
from dashboard.charts import arbitrage_chart, odds_chart
from dashboard.common import TIMEZONE, fair_link, points
from dashboard.series import POINTS

FAIR_COLUMN = st.column_config.LinkColumn(
    "Fair odds",
    display_text=r"#fair-odds-(.+)$",
    help="1 / probability, linked to this fixture at the book the probability came from "
    "(sportybet or msport).",
)
BET_COLUMN = st.column_config.LinkColumn("Bet", display_text="open ↗")


def for_event(doc: dict[str, Any], event_id: str) -> dict[str, Any]:
    """The parts of the signals document that belong to `event_id`."""
    arb, ev = doc.get("arbitrage") or {}, doc.get("ev") or {}
    prefix = f"{event_id}|"
    return {
        "legs": [leg for leg in arb.get("legs") or [] if leg["eventId"] == event_id],
        "tracks": {
            key[len(prefix) :]: track
            for key, track in (arb.get("tracks") or {}).items()
            if key.startswith(prefix)
        },
        "ev": [row for row in ev.get("rows") or [] if row["eventId"] == event_id],
        "prices": {
            key[len(prefix) :]: books
            for key, books in (ev.get("prices") or {}).items()
            if key.startswith(prefix)
        },
    }


def header(parts: dict[str, Any]) -> tuple[str, Any] | None:
    """(fixture name, kick-off) from the signals, for a fixture with no deep dive."""
    for row in [*parts["legs"], *parts["ev"]]:
        if row.get("fixture"):
            return str(row["fixture"]), row.get("kickoffAt")
    return None


def _local(values: Any) -> Any:
    return pd.to_datetime(values, utc=True).dt.tz_convert(TIMEZONE)


def _market(row: dict[str, Any]) -> str:
    return f"{row['market']}{' ' + row['line'] if row.get('line') else ''}"


def arbitrage_section(parts: dict[str, Any], kickoff_at: Any, played: bool) -> None:
    legs = pd.DataFrame(parts["legs"])
    st.subheader("Surebets on this fixture")
    if legs.empty:
        st.caption("No surebet has been detected on this fixture.")
        return
    st.caption(
        "Every market here carried a true surebet at some point: arbitrage above 1 with "
        "every leg priced within five minutes. The chart is that market's arbitrage over "
        "time, replayed from every stored price."
    )
    for market_id, rows in legs.groupby("marketId", sort=False):
        best = rows.loc[rows.arbitrage.idxmax()]
        newest_key = rows.loc[rows.detectedAt.idxmax(), "signalKey"]
        newest = rows[rows.signalKey == newest_key]
        detections = rows.drop_duplicates("signalKey")
        track = parts["tracks"].get(market_id)
        with st.container(border=True):
            st.markdown(f"**{_market(best)}**")
            m = st.columns(4)
            m[0].metric("Best detected", f"{best.arbitrage:.4f}")
            m[1].metric("Detections", f"{len(detections)}")
            last = (track or {}).get("last") or {}
            m[2].metric(
                "At kick-off" if played else "Now",
                "—" if last.get("arbitrage") is None else f"{last['arbitrage']:.4f}",
            )
            m[3].metric("Legs apart", f"{int(best.spreadSeconds)} s")
            table = newest.assign(detected=_local(newest.detectedAt))[
                ["outcome", "book", "odds", "latestOdds", "url", "detected"]
            ]
            st.dataframe(
                table,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "outcome": "Outcome",
                    "book": "Book",
                    "odds": st.column_config.NumberColumn("Odds at detection", format="%.2f"),
                    "latestOdds": st.column_config.NumberColumn("Last recorded", format="%.2f"),
                    "url": BET_COLUMN,
                    "detected": st.column_config.DatetimeColumn("Priced at", format="D MMM HH:mm"),
                },
            )
            if not track or not track.get("points"):
                st.caption("No tracked history for this market yet.")
                continue
            history = points(track["points"], "OBSERVED_AT", "ARBITRAGE", "LEG_SPREAD_SECONDS")
            marks = pd.DataFrame(
                {"DETECTED_AT": _local(detections.detectedAt), "ARBITRAGE": detections.arbitrage}
            )
            st.plotly_chart(
                arbitrage_chart(history, marks, kickoff_at, "kick-off" if played else "now"),
                use_container_width=True,
                key=f"event-arb-{market_id}",
            )
            st.caption(
                f"{int(track.get('total', len(history)))} recorded changes, drawn as the "
                f"~{POINTS} largest. Red diamonds: surebet detections. Grey points: legs more "
                "than five minutes apart."
            )


def ev_section(parts: dict[str, Any], kickoff_at: Any, played: bool) -> None:
    rows = pd.DataFrame(parts["ev"])
    st.subheader("Positive EV on this fixture")
    if rows.empty:
        st.caption("No positive-EV price has been detected on this fixture.")
        return
    st.caption(
        "Each row is a price that beat the most recent probability sportybet or msport "
        "published for the outcome. **Fair odds** opens this fixture at that book."
    )
    rows = rows.sort_values("detectedAt", ascending=False)
    # Documents published before the source link existed do not carry it.
    source_urls = rows["comparableUrl"] if "comparableUrl" in rows else [None] * len(rows)
    shown = rows.assign(
        market=[_market(r) for r in rows.to_dict("records")],
        detected=_local(rows.detectedAt),
        fair=[fair_link(u, p) for u, p in zip(source_urls, rows.impliedP, strict=True)],
    )[["market", "outcome", "book", "odds", "ev", "impliedP", "fair", "url", "detected", "verdict"]]
    st.dataframe(
        shown,
        hide_index=True,
        use_container_width=True,
        column_config={
            "market": "Market",
            "outcome": "Outcome",
            "book": "Book",
            "odds": st.column_config.NumberColumn("Odds", format="%.2f"),
            "ev": st.column_config.NumberColumn("EV", format="%.3f"),
            "impliedP": st.column_config.NumberColumn("Probability", format="percent"),
            "fair": FAIR_COLUMN,
            "url": BET_COLUMN,
            "detected": st.column_config.DatetimeColumn("Priced at", format="D MMM HH:mm"),
            "verdict": "Result",
        },
    )
    outcomes = rows.drop_duplicates(["marketId", "outcomeId", "book"])
    labels = [
        f"{_market(r)} · {r['outcome']} @ {r['book']} (best EV {r['ev']:.3f})"
        for r in outcomes.to_dict("records")
    ]
    picked = outcomes.iloc[labels.index(st.selectbox("Chart an outcome's prices", labels))]
    books = parts["prices"].get(f"{picked.marketId}|{picked.outcomeId}")
    if not books:
        st.caption("No price history extracted for this outcome yet.")
        return
    ticks = pd.concat(
        [
            points(series, "FIRE_TIME", "ODDS").assign(BOOKMAKER_NAME=book)
            for book, series in books.items()
        ],
        ignore_index=True,
    )
    mine = rows[(rows.marketId == picked.marketId) & (rows.outcomeId == picked.outcomeId)]
    marks = pd.DataFrame(
        {
            "DETECTED_AT": _local(mine.detectedAt),
            "ODDS": mine.odds,
            "EV": mine.ev,
            "BOOKMAKER_NAME": mine.book,
        }
    )
    x_end = (
        pd.Timestamp(kickoff_at)
        if played and kickoff_at is not None
        else pd.Timestamp.now(tz="UTC")
    )
    ticks = ticks[ticks.FIRE_TIME <= x_end]
    st.plotly_chart(
        odds_chart(ticks, picked.book, marks, kickoff_at, "positive EV", x_end),
        use_container_width=True,
        key="event-ev-chart",
    )
    st.caption(
        f"Every book's price for {picked.outcome}, each line its ~{POINTS} largest moves and "
        f"held until changed; {picked.book} in bold. Diamonds: positive-EV detections."
    )
