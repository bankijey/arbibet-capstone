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

import pandas as pd
import streamlit as st
from dashboard.backtest import paper_wallet, since, swings
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
    "Put 20% of the bankroll on every surebet of "
    f"{(wallet['surebetMin'] - 1):.1%} or more, split across its legs so the return is "
    "the same whichever outcome lands, and a quarter of Kelly on every positive-EV "
    "price, compounding. Surebets pay their arithmetic; EV bets pay their real result. "
    "Assumes each price was taken at detection, which is what an instant alert is for."
)

LOOKBACK = {"all": None, "7 days": 7, "14 days": 14, "30 days": 30, "60 days": 60}
c1, c2, _ = st.columns([1, 1, 3])
start_amount = float(
    c1.number_input(
        "Start with ($)",
        min_value=10.0,
        max_value=10_000_000.0,
        value=float(wallet["start"]),
        step=100.0,
        help="Every stake is a fraction of the bankroll, so the shape is the same at any size.",
    )
)
window = c2.selectbox(
    "Look back",
    list(LOOKBACK),
    index=0,
    help="Take only what was alerted from then on; the wallet opens at that point.",
)

bets = wallet.get("bets")
now = pd.Timestamp.now(tz="UTC")
cutoff = now - pd.Timedelta(days=LOOKBACK[window]) if LOOKBACK[window] else None
if bets:
    surebets = since(
        frame(
            bets["surebets"],
            {"detectedAt": "DETECTED_AT", "kickoffAt": "KICKOFF_AT", "arbitrage": "ARBITRAGE"},
        ),
        cutoff,
    )
    ev = since(
        frame(
            bets["ev"],
            {
                "detectedAt": "DETECTED_AT",
                "kickoffAt": "KICKOFF_AT",
                "ev": "EV",
                "odds": "ODDS",
                "verdict": "VERDICT",
            },
        ),
        cutoff,
    )
    run = paper_wallet(surebets, ev, now, start_amount)
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
        "from": surebets.DETECTED_AT.min() if not surebets.empty else cutoff,
    }
    curve = run.curve
else:  # an older document without the bets: show what was published
    shown = wallet
    curve = points(wallet["curve"], "AT", "BANKROLL")

m = st.columns(6)
m[0].metric("Bankroll now", f"${shown['final']:,.0f}", delta=f"${shown['profit']:+,.0f}")
m[1].metric(
    "Return on start",
    f"{shown['profit'] / shown['start']:+.1%}",
    help="Since the first surebet the wallet could have taken in the window.",
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
    st.plotly_chart(
        swing_chart(swings(curve, start_amount), start_amount), use_container_width=True
    )
    since_label = (
        pd.Timestamp(shown["from"]).tz_convert(TIMEZONE) if shown.get("from") is not None else None
    )
    st.caption(
        (f"From {since_label:%d %b} to now, in platform time. " if since_label is not None else "")
        + f"Total staked ${shown['staked']:,.0f}. Green steps are settlements that raised the "
        "bankroll, red ones lowered it; the shading is how far under its best the wallet stood."
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
