"""One fixture, in depth: its signals, how it was priced, what the sides had been doing.

THE event page. Reached from the slips page's deep-dive cards, the signal
tables' Event links and every Telegram "history" button, all of which pass
`?event_id=<uuid>`. A fixture with signals shows its surebets and EV over
time here (dashboard/event_signals.py) beside the deep dive below. A page per
fixture rather than a picker, because the answer to "is this slip sane?" is
usually about ONE match and a link is shareable.

Data: the fixture's deep-dive document in Supabase (`serving.dive`), built by
the runner from the warehouse -- the pre-match brief, the post-match note, the
busiest markets' price history reduced to their largest moves, both sides'
last ten matches before kick-off, how the same markets settled for them, and
what punters backed. Deep dives are kept for 45 days after kick-off.

Two sources, deliberately kept apart on the page:

* **The market** -- every book's published price over time. What the fixture
  was SOLD at.
* **The record** -- API-Football's match history for both sides. What the
  sides had actually been DOING.

The gap between the two is the entire point of the platform, and it is why
they are not blended into a single verdict. Only matches BEFORE kick-off are
shown: judging how a fixture was priced using what happened afterwards is the
most inviting mistake available on this page.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st
from dashboard import event_signals
from dashboard.common import at, dive, document, kickoff, points
from dashboard.series import POINTS

FORM_WINDOW = 10

# Team-table columns: label -> (match field, decimals per match).
FORM_NUMBERS = {
    "xG": ("xg", 2),
    "xGA": ("xga", 2),
    "Shots": ("shots", 0),
    "On target": ("shotsOn", 0),
    "Passes": ("passes", 0),
    "Poss %": ("possession", 0),
    "Corners": ("corners", 0),
    "Saves": ("saves", 0),
    "Blocked": ("blocked", 0),
    "Fouls": ("fouls", 0),
    "Cards": ("cards", 0),
}


def _num(v: object, digits: int = 0) -> str:
    return "—" if v is None or pd.isna(v) else f"{float(v):.{digits}f}"


def _pct(v: object) -> str:
    return "—" if v is None or pd.isna(v) else f"{float(v):.0%}"


back = st.columns([1, 1, 6])
back[0].page_link("views/overview.py", label="← market signals")
back[1].page_link("views/slips.py", label="← betting slips")

# A shared URL carries `?event_id=`; an in-app click hands it over in
# session_state, because switch_page clears the query string.
event_id = st.query_params.get("event_id") or st.session_state.get("deep_dive_event")
if event_id and "event_id" not in st.query_params:
    st.query_params["event_id"] = event_id
if not event_id:
    st.warning("No fixture selected. Open this page from a deep-dive card.")
    st.stop()

d = dive(str(event_id))
signals = event_signals.for_event(document("signals"), str(event_id))
if d is None:
    # A fixture with signals but no published deep dive yet: its signals are
    # still worth the page.
    head = event_signals.header(signals)
    if head is None:
        st.error(
            "Nothing published for this fixture. Event pages exist for fixtures with a "
            "signal or a booking slip, and are kept for 45 days after kick-off."
        )
        st.stop()
    kicked = at(head[1]) if head[1] else None
    st.title(head[0])
    if kicked is not None:
        st.caption(f"kick-off {kicked:%A %d %B %Y, %H:%M}")
    played = kicked is not None and kicked <= pd.Timestamp.now(tz="UTC")
    event_signals.arbitrage_section(signals, kicked, played)
    event_signals.ev_section(signals, kicked, played)
    st.info("The deep dive for this fixture is published with the next warm cycle.")
    st.stop()

kicked = at(d["kickoffAt"])
played = kicked <= pd.Timestamp.now(tz="UTC")
st.title(f"{d['home']} v {d['away']}")
st.caption(f"{d['tournament'] or 'competition unknown'} · kick-off {kicked:%A %d %B %Y, %H:%M}")

if not d["hasTeams"]:
    st.warning(
        "This fixture never resolved to API-Football team ids, so no history "
        "is available for it — the market section below still works. About a "
        "third of fixtures are in this state; they are priceable but have no "
        "record behind them."
    )

brief = d.get("brief")
if brief:
    with st.container(border=True):
        st.markdown(brief["summary"])
        st.caption(
            f"Written by `{brief['model']}` from {int(brief['markets'])} markets and "
            f"{int(brief['sides'])} sides' match history in this warehouse — and from "
            "nothing else. The model is told it has no other knowledge of these teams: "
            "no league position, no injuries, no past meetings, nothing it cannot be "
            f"shown below. {kickoff(brief['generatedAt'])}."
        )

result = d.get("result")
if result:
    with st.container(border=True):
        st.caption("After full time")
        st.markdown(result["summary"])
        st.caption(f"Written once by `{result['model']}`, {kickoff(result['generatedAt'])}.")

if signals["legs"] or signals["ev"]:
    st.divider()
    event_signals.arbitrage_section(signals, kicked, played)
    event_signals.ev_section(signals, kicked, played)

st.divider()
st.subheader("How the books priced it")

markets = d["markets"]
if not markets:
    st.info(
        "No price history for this fixture. Prices are extracted only for fixtures "
        "that produced a signal or were slipped, and bronze prunes after about seven weeks."
    )
else:
    labels = [
        f"{m['name']}{' ' + m['line'] if m.get('line') else ''}  ({int(m['ticks'])} ticks)"
        for m in markets
    ]
    default = next((i for i, m in enumerate(markets) if m["name"].lower() == "1x2"), 0)
    picked = markets[labels.index(st.selectbox("Market", labels, index=default))]
    parts = [
        points(series, "FIRE_TIME", "ODDS").assign(OUTCOME=outcome, BOOKMAKER_NAME=book)
        for outcome, books in picked["series"].items()
        for book, series in books.items()
    ]
    plotted = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if plotted.empty:
        st.info("No prices recorded for this market.")
    else:
        figure = px.line(
            plotted,
            x="FIRE_TIME",
            y="ODDS",
            color="BOOKMAKER_NAME",
            # One panel per outcome: overlaying every outcome of every book on
            # one axis is unreadable.
            facet_row="OUTCOME",
            line_shape="hv",
            labels={"FIRE_TIME": "", "ODDS": "odds", "BOOKMAKER_NAME": "book", "OUTCOME": ""},
            height=260 * max(plotted.OUTCOME.nunique(), 1),
        )
        figure.add_vline(x=kicked, line_dash="dash", line_color="#888", annotation_text="kick-off")
        figure.update_layout(margin={"t": 40, "b": 0, "l": 0, "r": 0}, hovermode="x unified")
        st.plotly_chart(figure, use_container_width=True)
        st.caption(
            f"{int(picked['ticks']):,} price changes across {picked['books']} books, each "
            f"book's line drawn as its ~{POINTS} largest moves. Drag to zoom, double-click "
            "to reset. The dashed line is kick-off: to the left the books are disagreeing "
            "about a match that has not happened, to the right they are reacting to one "
            f"that is. The {len(markets)} busiest markets are available."
        )

st.divider()
st.subheader("What both sides had been doing")
st.caption(
    f"The last {FORM_WINDOW} matches each side played BEFORE this kick-off, from "
    "API-Football. Later results are excluded deliberately: judging how a fixture was "
    "priced using what happened afterwards is the most inviting mistake on this page."
)

if d["hasTeams"]:
    for team in d["teams"]:
        st.markdown(f"**{team['name']}** — {team['role']} side")
        form = pd.DataFrame(team["matches"])
        if form.empty:
            st.info("No settled history for this side.")
            continue
        for column in ("possession", "passesPct"):
            form[column] = pd.to_numeric(form[column].astype(str).str.rstrip("%"), errors="coerce")
        form["shotAcc"] = form.shotsOn / form.shots.where(form.shots > 0)
        form["passAcc"] = (form.passesPct / 100).fillna(
            form.passesAccurate / form.passes.where(form.passes > 0)
        )
        form["cards"] = form.yellow.fillna(0) + form.red.fillna(0)
        # 'W', not 'win': the first dashboard compared against "win" and every
        # side on every page read "Won 0/10".
        wins = int((form.result == "W").sum())
        m = st.columns(7)
        m[0].metric("Won", f"{wins}/{len(form)}")
        m[1].metric("Scored", f"{form.goalsFor.mean():.1f}", help="Goals per match.")
        m[2].metric("Conceded", f"{form.goalsAgainst.mean():.1f}")
        m[3].metric("Clean sheets", f"{int((form.goalsAgainst == 0).sum())}/{len(form)}")
        xg = form.xg.dropna()
        m[4].metric(
            "xG",
            f"{xg.mean():.2f}" if len(xg) else "—",
            help="API-Football populates xG for some leagues only.",
        )
        shot_acc = form.shotAcc.dropna()
        m[5].metric(
            "Shot accuracy",
            f"{shot_acc.mean():.0%}" if len(shot_acc) else "—",
            help="Shots on target over total shots, per match, averaged.",
        )
        pass_acc = form.passAcc.dropna()
        m[6].metric("Pass accuracy", f"{pass_acc.mean():.0%}" if len(pass_acc) else "—")

        mean_row = {
            "Date": f"Mean · last {len(form)}",
            "Competition": "",
            "Opponent": "",
            "H/A": "",
            "Score": f"{form.goalsFor.mean():.1f}-{form.goalsAgainst.mean():.1f}",
            "Result": f"{wins}W",
            **{
                name: _num(form[col].mean(), max(digits, 1))
                for name, (col, digits) in FORM_NUMBERS.items()
            },
            "Shot acc": _pct(form.shotAcc.mean()),
            "Pass acc": _pct(form.passAcc.mean()),
        }
        match_rows = [
            {
                "Date": f"{at(r['date']):%d %b %y}",
                "Competition": r["competition"] or "",
                "Opponent": r["opponent"] or "",
                "H/A": "H" if r["isHome"] else "A",
                "Score": f"{_num(r['goalsFor'])}-{_num(r['goalsAgainst'])}",
                "Result": r["result"] or "",
                **{name: _num(r[col], digits) for name, (col, digits) in FORM_NUMBERS.items()},
                "Shot acc": _pct(r["shotAcc"]),
                "Pass acc": _pct(r["passAcc"]),
            }
            for _, r in form.iterrows()
        ]
        st.dataframe(
            pd.DataFrame([mean_row, *match_rows])[
                [
                    "Date",
                    "Competition",
                    "Opponent",
                    "H/A",
                    "Score",
                    "Result",
                    "xG",
                    "xGA",
                    "Shots",
                    "On target",
                    "Shot acc",
                    "Passes",
                    "Pass acc",
                    "Poss %",
                    "Corners",
                    "Saves",
                    "Blocked",
                    "Fouls",
                    "Cards",
                ]
            ],
            hide_index=True,
            use_container_width=True,
            column_config={
                "Blocked": st.column_config.TextColumn(
                    "Blocked", help="This side's shots that were blocked."
                ),
                "Saves": st.column_config.TextColumn(
                    "Saves", help="Saves by this side's goalkeeper."
                ),
            },
        )

    st.divider()
    st.subheader("How these markets have actually settled")
    st.caption(
        "The same markets the books price, resolved against real results for both "
        "sides over the same window. This is the number the slip verdicts are built "
        "on, and it is recent form — it knows nothing about who the opponent was."
    )
    settled = pd.DataFrame(d["settled"])
    if settled.empty:
        st.info("No settled market history for either side.")
    else:
        settled["rate"] = settled.landed / settled.of
        st.dataframe(
            settled[["side", "market", "period", "pick", "landed", "of", "rate"]].rename(
                columns={
                    "side": "Side",
                    "market": "Market",
                    "period": "Period",
                    "pick": "Pick",
                    "landed": "Landed",
                    "of": "Of",
                    "rate": "Rate",
                }
            ),
            hide_index=True,
            use_container_width=True,
            # `format="percent"`, not "%.0f%%": the value is a 0-1 fraction.
            column_config={
                "Rate": st.column_config.ProgressColumn(
                    "Rate", min_value=0.0, max_value=1.0, format="percent"
                )
            },
        )

st.divider()
st.subheader("What punters actually backed here")
st.caption(
    "Every booking slip that names this fixture, rolled up by pick: how many slips "
    "chose it, how many times those slips were copied, and at what price."
)
punters = d.get("punters")
if not punters or not punters["picks"]:
    st.info("No booking slip in the corpus names this fixture.")
else:
    by_pick = pd.DataFrame(punters["picks"])
    top = by_pick.iloc[0]
    m = st.columns(5)
    m[0].metric("Slips", f"{punters['slips']:,}", help="Distinct slips naming this fixture.")
    m[1].metric("Copied", f"{punters['copies']:,}", help="Copies across those slips.")
    m[2].metric("Median copies per slip", f"{punters['medianCopies']:,.0f}")
    m[3].metric("Distinct picks", f"{len(by_pick)}")
    m[4].metric(
        "Most backed",
        f"{top.slips / punters['slips']:.0%}",
        help=f"{top.pick}: on {int(top.slips)} of {punters['slips']} slips.",
    )
    st.caption(f"Most backed pick: **{top.pick}**.")
    by_pick["share"] = by_pick.slips / punters["slips"]
    by_pick["form"] = [
        f"{int(w)}/{int(n)}" if pd.notna(n) and n else "no history"
        for w, n in zip(by_pick.wins, by_pick.matches, strict=True)
    ]
    by_pick["result"] = by_pick.result.fillna("")
    st.dataframe(
        by_pick[["pick", "slips", "share", "copies", "medianOdds", "form", "result"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "pick": "Pick",
            "slips": st.column_config.NumberColumn("Slips", format="%d"),
            "share": st.column_config.ProgressColumn(
                "Share of slips", min_value=0.0, max_value=1.0, format="percent"
            ),
            "copies": st.column_config.NumberColumn("Copies", format="%d"),
            "medianOdds": st.column_config.NumberColumn("Median odds", format="%.2f"),
            "form": "Last 10",
            "result": "Result",
        },
    )
