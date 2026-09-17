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
import plotly.express as px
import streamlit as st
from dashboard.common import TIMEZONE, document, points, published_at, versions

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

st.subheader("A paper wallet on every signal")
st.caption(
    f"Start with ₦{wallet['start']:,.0f}. Put 20% of the bankroll on every surebet of "
    f"{(wallet['surebetMin'] - 1):.1%} or more, split across its legs so the return is "
    "the same whichever outcome lands, and a quarter of Kelly on every positive-EV "
    "price. Compound. Surebets pay their arithmetic; EV bets pay their real result. "
    "Assumes each price was taken at detection, which is what an instant alert is for."
)
m = st.columns(6)
m[0].metric("Bankroll now", f"₦{wallet['final']:,.0f}", delta=f"₦{wallet['profit']:+,.0f}")
m[1].metric(
    "Return on start",
    f"{wallet['profit'] / wallet['start']:+.1%}",
    help="Since the first surebet the wallet could have taken.",
)
m[2].metric("Surebets taken", f"{wallet['surebets']:,}")
m[3].metric(
    "EV bets",
    f"{wallet['evBets']:,}",
    help=f"{wallet['evWon']:,} won. Only settled EV bets are counted.",
)
m[4].metric("Worst drawdown", f"{wallet['maxDrawdown']:.1%}")
m[5].metric("Open positions", f"{wallet['openBets']:,}", help="Placed, not yet kicked off.")

curve = points(wallet["curve"], "AT", "BANKROLL")
if curve.empty:
    st.caption("No settled bet yet.")
else:
    fig = px.line(curve, x="AT", y="BANKROLL", height=320)
    fig.add_hline(y=wallet["start"], line_dash="dot", line_color="grey")
    fig.update_layout(
        margin={"t": 10, "b": 0, "l": 0, "r": 0}, xaxis_title="", yaxis_title="bankroll (₦)"
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        f"From {pd.Timestamp(wallet['from']).tz_convert(TIMEZONE):%d %b} to now, in "
        f"platform time. Total staked ₦{wallet['staked']:,.0f}."
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
