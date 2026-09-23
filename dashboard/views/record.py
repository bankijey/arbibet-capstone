"""Track record: the case for the signals, before the signals.

Two things a visitor should see first:

1. **The paper wallet.** One bankroll placed on every signal the platform
   would have alerted -- surebets of 1.5% or more, and every positive-EV price
   at a quarter of Kelly -- compounding, settled at kick-off plus two hours.
   Every point on the curve is arithmetic on stored prices and real results.
2. **How copied slips fared.** The most-copied booking slips, settled leg by
   leg. Their combined probability is what it is; the copies are what people
   did anyway.

Data: the 'record' section of the 'signals' document.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st
from dashboard.backtest import best_per_event, choose, paper_wallet, swings, window
from dashboard.charts import swing_chart
from dashboard.common import TIMEZONE, document, frame, points, published_at, versions

BOT = "https://t.me/Arbibbobobot"

doc = document("signals")
record = doc.get("record")
if not record:
    st.info("The track record has not been published yet.")
    st.stop()
wallet, slips = record["wallet"], record["slips"]

st.title("Track record")
st.caption(
    "What placing every signal this platform raised would have done, and how the "
    "most-copied booking slips did over the same weeks. Nothing here is a forecast: "
    "every figure is stored prices and settled results, published "
    f"{published_at(versions().get('signals'))}."
)

# --- the paper wallet ---------------------------------------------------------------

st.subheader("Paper wallet")
st.caption(
    "A strategy simulator on the platform's own signals: pick which alerts the "
    "wallet takes, the thresholds, the sizing and the time window, and it re-runs "
    "the compounding bankroll bet by bet. A surebet's stake is split across its "
    "legs so the return is the same whichever outcome lands and pays its "
    "arithmetic; EV bets pay their real result. Assumes each price was taken at "
    "detection, which is what an instant alert is for."
)

bets = wallet.get("bets")
now = pd.Timestamp.now(tz="UTC")
if bets:
    surebets_all = frame(
        bets["surebets"],
        {
            "eventId": "EVENT_ID",
            "detectedAt": "DETECTED_AT",
            "kickoffAt": "KICKOFF_AT",
            "arbitrage": "ARBITRAGE",
            "fixture": "FIXTURE",
        },
        times=("DETECTED_AT", "KICKOFF_AT"),
    )
    ev_all = frame(
        bets["ev"],
        {
            "eventId": "EVENT_ID",
            "marketId": "MARKET_ID",
            "outcomeId": "OUTCOME_ID",
            "detectedAt": "DETECTED_AT",
            "kickoffAt": "KICKOFF_AT",
            "ev": "EV",
            "odds": "ODDS",
            "verdict": "VERDICT",
            "fixture": "FIXTURE",
            "pick": "PICK",
            "book": "BOOK",
        },
        times=("DETECTED_AT", "KICKOFF_AT"),
    )

    c1, c2, c3, c4 = st.columns([1.2, 1.3, 1, 1])
    start_amount = float(
        c1.number_input(
            "Bankroll ($)",
            min_value=10.0,
            max_value=10_000_000.0,
            value=float(wallet["start"]),
            step=100.0,
            help="Every stake is a fraction of the bankroll, so the shape is the same at any size.",
        )
    )
    kind = c2.segmented_control(
        "Signals",
        ["Both", "Surebets", "EV"],
        default="Both",
        help="Which alerts the wallet takes.",
    )
    floor = float(wallet.get("surebetFloor") or 1.005)
    arb_min = float(
        c3.number_input(
            "Surebets ≥",
            min_value=floor,
            max_value=1.2,
            value=1.012,
            step=0.001,
            format="%.3f",
            help="Take only surebets whose guaranteed multiple is at least this. "
            "1.012 = 1.2% locked in.",
            disabled=kind == "EV",
        )
    )
    ev_min = float(
        c4.number_input(
            "EV ≥",
            min_value=0.0,
            max_value=0.5,
            value=0.015,
            step=0.005,
            format="%.3f",
            help="Take only EV bets with at least this edge. 0.015 = 1.5%, the bot's default.",
            disabled=kind == "Surebets",
        )
    )

    SIZINGS = {
        "quarter Kelly": "quarter_kelly",
        "half Kelly": "half_kelly",
        "full Kelly": "kelly",
        "flat 1% of start": "flat",
        "2% of bankroll": "fixed_2pct",
    }
    ENTRIES = {"first seen": "first", "highest EV": "best", "last seen": "last"}
    s1, s2, s3, s4 = st.columns([1.3, 1.1, 1, 1.2])
    ev_sizing = SIZINGS[
        s1.selectbox(
            "EV sizing",
            list(SIZINGS),
            index=0,
            help="How much of the bankroll each EV bet takes. Kelly is ev/(odds−1); "
            "the fractions hedge a borrowed probability.",
            disabled=kind == "Surebets",
        )
    ]
    entry = ENTRIES[
        s2.selectbox(
            "EV entry",
            list(ENTRIES),
            index=0,
            help="An opportunity is often detected many times as the price moves; this "
            "picks which detection the wallet takes: the first it saw, the one with the "
            "highest EV, or the last before kick-off.",
            disabled=kind == "Surebets",
        )
    ]
    one_per_event = s4.toggle(
        "One bet per fixture",
        value=True,
        help="At most one bet per match: the best opportunity on it (highest EV, or "
        "highest arbitrage for surebets). Several markets of one match settle on the "
        "same result, so taking them all stacks correlated stakes on one outcome. "
        "Off: every opportunity is taken.",
    )
    surebet_fraction = (
        float(
            s3.number_input(
                "Surebet stake %",
                min_value=1.0,
                max_value=50.0,
                value=20.0,
                step=1.0,
                help="The share of the bankroll each surebet takes, split across its legs.",
                disabled=kind == "EV",
            )
        )
        / 100.0
    )

    # The window: two handles, hour steps, from the earliest published bet (the
    # publisher keeps up to `windowDays`, 90 by default) to now; opens on the
    # last two weeks. The wallet starts at the left handle and is valued at the
    # right one, so bets still open there show as open positions.
    detected = pd.concat([surebets_all.DETECTED_AT, ev_all.DETECTED_AT])
    earliest = detected.min() if detected.notna().any() else now - pd.Timedelta(days=14)
    window_days = int(wallet.get("windowDays") or 90)
    lo = max(pd.Timestamp(earliest).floor("h"), now - pd.Timedelta(days=window_days))
    hi = now.ceil("h")
    default_start = max(lo, hi - pd.Timedelta(days=14))
    start_at, end_at = st.slider(
        "Window",
        min_value=lo.tz_convert(TIMEZONE).to_pydatetime(),
        max_value=hi.tz_convert(TIMEZONE).to_pydatetime(),
        value=(
            default_start.tz_convert(TIMEZONE).to_pydatetime(),
            hi.tz_convert(TIMEZONE).to_pydatetime(),
        ),
        step=timedelta(hours=1),
        format="D MMM HH:mm",
        help="Drag either handle; hour steps. The wallet takes only signals raised "
        "inside the window and is valued at its end.",
    )
    start_ts, end_ts = (
        pd.Timestamp(start_at).tz_convert("UTC"),
        min(pd.Timestamp(end_at).tz_convert("UTC"), now),
    )

    surebets = (
        surebets_all[surebets_all.ARBITRAGE >= arb_min] if kind != "EV" else surebets_all.iloc[0:0]
    )
    ev = ev_all[ev_all.EV >= ev_min] if kind != "Surebets" else ev_all.iloc[0:0]
    surebets, ev = window(surebets, start_ts, end_ts), window(ev, start_ts, end_ts)
    # One detection per opportunity at the chosen entry; older documents carry
    # only first-seen rows, which choose() then leaves as they are.
    if not ev.empty and "MARKET_ID" in ev.columns and ev.MARKET_ID.notna().any():
        ev = choose(ev, entry)
    if one_per_event:
        surebets, ev = best_per_event(surebets, "ARBITRAGE"), best_per_event(ev, "EV")
    run = paper_wallet(
        surebets, ev, end_ts, start_amount, ev_sizing=ev_sizing, surebet_fraction=surebet_fraction
    )
    taken = pd.concat([surebets.DETECTED_AT, ev.DETECTED_AT])
    shown = {
        "start": start_amount,
        "final": run.final,
        "profit": run.profit,
        "surebets": run.surebets,
        "evBets": run.ev_bets,
        "evWon": run.ev_won,
        "maxDrawdown": run.max_drawdown,
        "openBets": run.open_bets,
        "staked": run.staked,
        "from": taken.min() if taken.notna().any() else start_ts,
        "to": end_ts,
    }
    curve = run.curve
    entries = run.entries
else:  # an older document without the bets: show what was published
    shown = wallet
    curve = points(wallet["curve"], "AT", "BANKROLL")
    entries = None

m = st.columns(6)
m[0].metric("Bankroll now", f"${shown['final']:,.0f}", delta=f"${shown['profit']:+,.0f}")
m[1].metric(
    "Return on start",
    f"{shown['profit'] / shown['start']:+.1%}",
    help="Since the first signal the wallet could have taken in the window.",
)
m[2].metric("Surebets taken", f"{shown['surebets']:,}")
m[3].metric(
    "EV bets",
    f"{shown['evBets']:,}",
    help=f"{shown['evWon']:,} won. Only settled EV bets are counted.",
)
m[4].metric(
    "Worst drawdown", f"{shown['maxDrawdown']:.1%}", help="The deepest fall from a running peak."
)
m[5].metric("Open positions", f"{shown['openBets']:,}", help="Placed, not yet settled.")

if curve.empty:
    st.caption("No settled bet in this window.")
else:
    if entries is not None and not entries.empty:
        entries = entries.copy()
        # One line naming the bet, for the marker hover: the fixture, then what
        # was backed at what price, or the surebet's locked-in edge.
        entries["WHAT"] = [
            (
                f"<b>{e.FIXTURE or 'surebet'}</b><br>surebet · {e.EDGE:.1%} locked in"
                if e.KIND == "surebet"
                else f"<b>{e.FIXTURE or ''}</b><br>{e.PICK or '?'} @ {e.ODDS:.2f}"
                f" · {e.BOOK or ''} · EV {e.EDGE:+.1%} · {e.VERDICT}"
            )
            for e in entries.itertuples(index=False)
        ]
        entries["SETTLED_AT"] = pd.to_datetime(entries.SETTLED_AT, utc=True).dt.tz_convert(TIMEZONE)
    st.plotly_chart(
        swing_chart(swings(curve, start_amount), start_amount, entries=entries),
        use_container_width=True,
    )
    since_label = (
        pd.Timestamp(shown["from"]).tz_convert(TIMEZONE) if shown.get("from") is not None else None
    )
    to_label = (
        pd.Timestamp(shown["to"]).tz_convert(TIMEZONE) if shown.get("to") is not None else None
    )
    span = (
        f"From {since_label:%d %b %H:%M} to "
        + (f"{to_label:%d %b %H:%M}" if to_label is not None else "now")
        + ", in platform time. "
        if since_label is not None
        else ""
    )
    st.caption(
        span + f"Total staked ${shown['staked']:,.0f}. Green steps are settlements that raised the "
        "bankroll, red ones lowered it; the shading is how far under its best the wallet stood. "
        "Each dot is one settled bet — hover it for the fixture, the pick and what it did."
    )
    if entries is not None and not entries.empty:
        with st.expander(f"Every bet this strategy took · {len(entries)}", expanded=False):
            st.caption(
                "In settlement order: what was backed, at what price and edge, the stake the "
                "strategy chose, and the bankroll after it settled. Entry is the detection "
                "time — the moment the alert would have fired."
            )
            table = entries.copy()
            table["Entered"] = pd.to_datetime(table.DETECTED_AT, utc=True).dt.tz_convert(TIMEZONE)
            table["Edge"] = table.EDGE.map(lambda e: f"{e:+.1%}")
            st.dataframe(
                table[
                    [
                        "Entered",
                        "KIND",
                        "FIXTURE",
                        "PICK",
                        "BOOK",
                        "ODDS",
                        "Edge",
                        "VERDICT",
                        "STAKE",
                        "PROFIT",
                        "BANKROLL",
                    ]
                ].rename(
                    columns={
                        "KIND": "Signal",
                        "FIXTURE": "Fixture",
                        "PICK": "Pick",
                        "BOOK": "Book",
                        "ODDS": "Odds",
                        "VERDICT": "Result",
                        "STAKE": "Stake",
                        "PROFIT": "Profit",
                        "BANKROLL": "Bankroll after",
                    }
                ),
                column_config={
                    "Entered": st.column_config.DatetimeColumn(format="D MMM HH:mm"),
                    "Odds": st.column_config.NumberColumn(format="%.2f"),
                    "Stake": st.column_config.NumberColumn(format="$%.2f"),
                    "Profit": st.column_config.NumberColumn(format="$%.2f"),
                    "Bankroll after": st.column_config.NumberColumn(format="$%.0f"),
                },
                hide_index=True,
                use_container_width=True,
                height=320,
            )

# --- copied slips ---------------------------------------------------------------------

st.subheader("What the copied slips did")
if slips["settled"] == 0:
    st.caption("No fully settled slips yet.")
else:
    won_rate = slips["won"] / slips["settled"]
    copies_rate = slips["copiesWon"] / slips["copies"] if slips["copies"] else None
    leg_rate = slips["legsWon"] / slips["legs"] if slips["legs"] else None
    c = st.columns(4)
    c[0].metric("Slips settled", f"{slips['settled']:,}")
    c[1].metric(
        "Slips that won",
        f"{won_rate:.0%}",
        help="Every leg won. One lost leg loses the slip.",
    )
    c[2].metric(
        "Copies that won",
        f"{copies_rate:.0%}" if copies_rate is not None else "—",
        help=f"Weighted by how many people copied each slip: {slips['copies']:,.0f} copies.",
    )
    c[3].metric(
        "Legs that won",
        f"{leg_rate:.0%}" if leg_rate is not None else "—",
        help="Most legs win. The slip is the product of all of them.",
    )
    st.caption(
        f"Median combined odds {slips['medianOdds']:,.1f}. A slip's legs mostly land; "
        "multiplied together they mostly do not, and the price already says so. "
        "The signals above are the opposite construction: one price at a time, "
        "measured against its probability or against another book's price."
    )

st.divider()
st.markdown(
    "The same signals arrive on Telegram within seconds of a price, with the stake "
    f"split sized to your own balances at each book: [@Arbibbobobot]({BOT}). "
    "Market data, not betting advice."
)
