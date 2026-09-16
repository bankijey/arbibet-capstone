"""One fixture, in depth: how it was priced, and what the sides had been doing.

Reached from the front page's deep-dive cards, which pass `?event_id=<uuid>`.
A page per fixture rather than a picker, because the answer to "is this slip
sane?" is usually about ONE match and a link is shareable.

Two sources, deliberately kept apart on the page:

* **The market** -- every book's published price over time, from
  `fact_odds_tick`. This is what the fixture was SOLD at.
* **The record** -- API-Football's own match history for both sides, from
  `fact_team_match` and `fact_team_market_result`. This is what the sides had
  actually been DOING.

The gap between the two is the entire point of the platform, and it is why they
are not blended into a single verdict. A price is a claim; a record is
evidence; presenting them as one number would hide which is which.

Only matches BEFORE kick-off are shown. Using a side's later results to judge
how a fixture was priced is the most inviting mistake available on this page.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st
from dashboard.common import query
from dashboard.series import POINTS, reduce_steps

FORM_WINDOW = 10

# Team-table columns: label -> (fact_team_match column, decimals per match).
FORM_NUMBERS = {
    "xG": ("XG", 2),
    "xGA": ("XG_AGAINST", 2),
    "Shots": ("TOTAL_SHOTS", 0),
    "On target": ("SHOTS_ON", 0),
    "Passes": ("PASSES_TOTAL", 0),
    "Poss %": ("POSSESSION", 0),
    "Corners": ("CORNERS", 0),
    "Saves": ("SAVES", 0),
    "Blocked": ("BLOCKED_SHOTS", 0),
    "Fouls": ("FOULS", 0),
    "Cards": ("CARDS", 0),
}


def _num(v: object, digits: int = 0) -> str:
    return "—" if pd.isna(v) else f"{float(v):.{digits}f}"


def _pct(v: object) -> str:
    return "—" if pd.isna(v) else f"{float(v):.0%}"


st.page_link("views/slips.py", label="← back to betting slips")

# Two ways in. A shared/bookmarked URL carries `?event_id=`; an in-app click
# from a deep-dive card hands it over in session_state, because switch_page
# clears the query string. When it arrives by session_state, restore it to the
# URL so the page is shareable and survives reruns.
event_id = st.query_params.get("event_id") or st.session_state.get("deep_dive_event")
if event_id and "event_id" not in st.query_params:
    st.query_params["event_id"] = event_id
if not event_id:
    st.warning("No fixture selected. Open this page from a deep-dive card.")
    st.stop()

# Parameterised by a UUID from our own warehouse, but quoted defensively: it
# arrives from the URL bar, where anyone can type.
safe_event = str(event_id).replace("'", "")

fixture = query(
    f"""
    -- ::TIMESTAMP_LTZ renders in session time (Europe/Berlin); the stored
    -- TIMESTAMP_TZ keeps its writer's offset. See
    -- dbt/macros/platform_time.sql -- an evening Ligue 1 kick-off showed
    -- as 11:45 on this page before the cast.
    SELECT event_id, home_team, away_team, tournament,
           kickoff_at::TIMESTAMP_LTZ AS kickoff_at,
           home_team_id, away_team_id, apifootball_id
    FROM CORE.dim_fixture WHERE event_id = '{safe_event}'
    """
)
if fixture.empty:
    st.error(f"No fixture `{safe_event}`. Fixtures roll out of the loader window.")
    st.stop()

f = fixture.iloc[0]
# A match that finished hours ago cannot change: its prices, both sides' history
# before it and its slips are fixed. Those queries are cached against a constant
# instead of the warehouse watermark, so the half-hourly pipeline does not throw
# away a played deep dive that is exactly as true as before. Three hours covers
# extra time and the last in-play payloads.
played = pd.Timestamp(f.KICKOFF_AT) < pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=3)
st.title(f"{f.HOME_TEAM} v {f.AWAY_TEAM}")
st.caption(
    f"{f.TOURNAMENT or 'competition unknown'} · " f"kick-off {f.KICKOFF_AT:%A %d %B %Y, %H:%M}"
)

if pd.isna(f.HOME_TEAM_ID) or pd.isna(f.AWAY_TEAM_ID):
    st.warning(
        "This fixture never resolved to API-Football team ids, so no history "
        "is available for it — the market section below still works. About a "
        "third of fixtures are in this state; they are priceable but have no "
        "record behind them."
    )

# --- the brief -------------------------------------------------------------
# First, because a reader wants the shape of the thing before the tables. The
# newest brief per fixture: gold_fixture_summary_ai is keyed on
# (event_id, evidence_signature), so a fixture whose prices or history moved
# gains a ROW rather than replacing one, and without QUALIFY this would show
# the stale brief above its replacement.
brief = query(
    f"""
    SELECT summary, model, markets_described, sides_with_history,
           generated_at::TIMESTAMP_LTZ AS generated_at
    FROM CORE.gold_fixture_summary_ai
    WHERE event_id = '{safe_event}'
    QUALIFY row_number() OVER (ORDER BY generated_at DESC) = 1
    """
)
if not brief.empty:
    b = brief.iloc[0]
    with st.container(border=True):
        st.markdown(b.SUMMARY)
        st.caption(
            f"Written by `{b.MODEL}` from {int(b.MARKETS_DESCRIBED)} markets and "
            f"{int(b.SIDES_WITH_HISTORY)} sides' match history in this "
            f"warehouse — and from nothing else. The model is told it has no "
            f"other knowledge of these teams: no league position, no injuries, "
            f"no past meetings, nothing it cannot be shown below. "
            f"{b.GENERATED_AT:%d %b %H:%M}."
        )

# A finished match's result note, if one was written. Below the pre-match brief
# on purpose: the brief says what the market expected, this says what happened.
result_note = query(
    f"""
    SELECT summary, model, generated_at::TIMESTAMP_LTZ AS generated_at
    FROM CORE.gold_fixture_result_ai
    WHERE event_id = '{safe_event}'
    """
)
if not result_note.empty:
    r = result_note.iloc[0]
    with st.container(border=True):
        st.caption("After full time")
        st.markdown(r.SUMMARY)
        st.caption(f"Written once by `{r.MODEL}`, {r.GENERATED_AT:%d %b %H:%M}.")

st.divider()

st.subheader("How the books priced it")

markets = query(
    f"""
    SELECT market_id, market_name, line, count(*) AS ticks
    FROM ANALYTICS.stg_odds_tick
    WHERE event_id = '{safe_event}'
    GROUP BY 1, 2, 3
    ORDER BY ticks DESC
    """,
    stable=played,
)

if markets.empty:
    st.info(
        "No price history for this fixture. Bronze prunes at roughly seven "
        "weeks, and `odds/ticks.py` only extracts fixtures that either "
        "produced a signal or appear on this list."
    )
else:
    labels = [
        f"{r.MARKET_NAME}{' ' + r.LINE if r.LINE else ''}  ({int(r.TICKS)} ticks)"
        for _, r in markets.iterrows()
    ]
    picked = markets.iloc[labels.index(st.selectbox("Market", labels))]

    ticks = query(
        f"""
        SELECT outcome, bookmaker_name, odds, fire_time
        FROM ANALYTICS.stg_odds_tick
        WHERE event_id = '{safe_event}' AND market_id = '{picked.MARKET_ID}'
        ORDER BY fire_time
        """,
        stable=played,
    )
    # Each book's line per outcome drawn as its ~10 largest moves: a busy market
    # runs to hundreds of ticks, and the moves that matter drown in one-rung
    # reprices otherwise.
    plotted = reduce_steps(
        ticks, x="FIRE_TIME", y="ODDS", by=["OUTCOME", "BOOKMAKER_NAME"], points=POINTS
    )
    figure = px.line(
        plotted,
        x="FIRE_TIME",
        y="ODDS",
        color="BOOKMAKER_NAME",
        # One panel per outcome. On a deep dive the question is how the whole
        # market moved, not one side of it, and overlaying every outcome of
        # every book on one axis is unreadable.
        facet_row="OUTCOME",
        # A price holds until the book changes it. See the main page.
        line_shape="hv",
        labels={
            "FIRE_TIME": "",
            "ODDS": "odds",
            "BOOKMAKER_NAME": "book",
            "OUTCOME": "",
        },
        height=260 * max(plotted.OUTCOME.nunique(), 1),
    )
    figure.add_vline(
        x=f.KICKOFF_AT,
        line_dash="dash",
        line_color="#888",
        annotation_text="kick-off",
    )
    figure.update_layout(margin={"t": 40, "b": 0, "l": 0, "r": 0}, hovermode="x unified")
    st.plotly_chart(figure, use_container_width=True)
    st.caption(
        f"{len(ticks):,} price changes across {ticks.BOOKMAKER_NAME.nunique()} "
        f"books, each book's line drawn as its ~{POINTS} largest moves. Drag to "
        "zoom, double-click to reset. The dashed line is kick-off: to the left "
        "the books are disagreeing about a match that has not happened, to the "
        "right they are reacting to one that is."
    )

st.divider()

# --- what the sides had been doing ----------------------------------------
st.subheader("What both sides had been doing")
st.caption(
    f"The last {FORM_WINDOW} matches each side played BEFORE this kick-off, "
    "from API-Football. Later results are excluded deliberately: judging how a "
    "fixture was priced using what happened afterwards is the most inviting "
    "mistake on this page."
)

if pd.notna(f.HOME_TEAM_ID) and pd.notna(f.AWAY_TEAM_ID):
    for team_id, team_name, role in (
        (int(f.HOME_TEAM_ID), f.HOME_TEAM, "home"),
        (int(f.AWAY_TEAM_ID), f.AWAY_TEAM, "away"),
    ):
        st.markdown(f"**{team_name}** — {role} side")
        form = query(
            f"""
            SELECT match_date::TIMESTAMP_LTZ AS match_date,
                   league_name, opponent_name, is_home, result,
                   goals_for, goals_against, xg, xg_against, possession, corners,
                   total_shots, shots_on, passes_total, passes_accurate, passes_pct,
                   saves, blocked_shots, fouls, yellow, red
            FROM CORE.fact_team_match
            WHERE team_id = {team_id} AND match_date < '{f.KICKOFF_AT:%Y-%m-%d}'
            ORDER BY match_date DESC
            LIMIT {FORM_WINDOW}
            """,
            stable=played,
        )
        if form.empty:
            st.info("No settled history for this side.")
            continue

        # Accuracy per match, as a fraction. Shots on target over total shots;
        # passes from the provider's own completed-pass percentage, falling back
        # to accurate over total where it left the percentage blank.
        form["SHOT_ACC"] = form.SHOTS_ON / form.TOTAL_SHOTS.where(form.TOTAL_SHOTS > 0)
        pct = pd.to_numeric(form.PASSES_PCT.astype(str).str.rstrip("%"), errors="coerce") / 100
        form["PASS_ACC"] = pct.fillna(
            form.PASSES_ACCURATE / form.PASSES_TOTAL.where(form.PASSES_TOTAL > 0)
        )
        form["CLEAN_SHEET"] = form.GOALS_AGAINST == 0
        form["CARDS"] = form.YELLOW.fillna(0) + form.RED.fillna(0)

        # 'W', not 'win'. The first version compared against "win" and every
        # side on every page read "Won 0/10" -- a plausible number rather than
        # an error, which is how this codebase's bugs usually present.
        wins = int((form.RESULT == "W").sum())
        m = st.columns(7)
        m[0].metric("Won", f"{wins}/{len(form)}")
        m[1].metric("Scored", f"{form.GOALS_FOR.mean():.1f}", help="Goals per match.")
        m[2].metric("Conceded", f"{form.GOALS_AGAINST.mean():.1f}")
        m[3].metric("Clean sheets", f"{int(form.CLEAN_SHEET.sum())}/{len(form)}")
        # xG is populated for some leagues and not others. Showing a mean of
        # nothing would be a lie, so it says so.
        xg = form.XG.dropna()
        m[4].metric(
            "xG",
            f"{xg.mean():.2f}" if len(xg) else "—",
            help="API-Football populates xG for some leagues only.",
        )
        shot_acc = form.SHOT_ACC.dropna()
        m[5].metric(
            "Shot accuracy",
            f"{shot_acc.mean():.0%}" if len(shot_acc) else "—",
            help="Shots on target over total shots, per match, averaged.",
        )
        pass_acc = form.PASS_ACC.dropna()
        m[6].metric("Pass accuracy", f"{pass_acc.mean():.0%}" if len(pass_acc) else "—")

        poss = pd.to_numeric(form.POSSESSION.astype(str).str.rstrip("%"), errors="coerce")
        form["POSSESSION"] = poss
        mean_row = {
            "Date": f"Mean · last {len(form)}",
            "Competition": "",
            "Opponent": "",
            "H/A": "",
            "Score": f"{form.GOALS_FOR.mean():.1f}-{form.GOALS_AGAINST.mean():.1f}",
            "Result": f"{wins}W",
            **{
                name: _num(form[col].mean(), max(digits, 1))
                for name, (col, digits) in FORM_NUMBERS.items()
            },
            "Shot acc": _pct(form.SHOT_ACC.mean()),
            "Pass acc": _pct(form.PASS_ACC.mean()),
        }
        match_rows = [
            {
                "Date": f"{r.MATCH_DATE:%d %b %y}",
                "Competition": r.LEAGUE_NAME or "",
                "Opponent": r.OPPONENT_NAME or "",
                "H/A": "H" if r.IS_HOME else "A",
                "Score": f"{_num(r.GOALS_FOR)}-{_num(r.GOALS_AGAINST)}",
                "Result": r.RESULT or "",
                **{name: _num(r[col], digits) for name, (col, digits) in FORM_NUMBERS.items()},
                "Shot acc": _pct(r.SHOT_ACC),
                "Pass acc": _pct(r.PASS_ACC),
            }
            for _, r in form.iterrows()
        ]
        # The mean row first, then each match. Formatted as text so a mean can
        # carry a decimal while a match's shot count does not.
        table = pd.DataFrame([mean_row, *match_rows])
        st.dataframe(
            table[
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

    # --- how the markets themselves have settled --------------------------
    st.subheader("How these markets have actually settled")
    st.caption(
        "The same betradar markets the books price, resolved against real "
        "results for both sides over the same window. This is the number the "
        "slip verdicts are built on, and it is recent form — it knows nothing "
        "about who the opponent was."
    )
    settled = query(
        f"""
        WITH recent AS (
            SELECT team_id, market_family, period, side_or_line, verdict,
                   row_number() OVER (
                       PARTITION BY team_id, market_family, period, side_or_line
                       ORDER BY match_date DESC
                   ) AS recency
            FROM CORE.fact_team_market_result
            WHERE team_id IN ({int(f.HOME_TEAM_ID)}, {int(f.AWAY_TEAM_ID)})
              AND match_date < '{f.KICKOFF_AT:%Y-%m-%d}'
        )
        SELECT
          CASE WHEN team_id = {int(f.HOME_TEAM_ID)} THEN '{f.HOME_TEAM}'
               ELSE '{f.AWAY_TEAM}' END                       AS side,
          market_family, period, side_or_line,
          count(*)                                            AS matches,
          sum(CASE WHEN verdict = 'won' THEN 1 ELSE 0 END)    AS landed
        FROM recent
        WHERE recency <= {FORM_WINDOW}
        GROUP BY 1, 2, 3, 4
        HAVING count(*) >= 5
        ORDER BY landed / matches DESC, matches DESC
        LIMIT 40
        """,
        stable=played,
    )
    if settled.empty:
        st.info("No settled market history for either side.")
    else:
        settled["rate"] = settled.LANDED / settled.MATCHES
        st.dataframe(
            settled[
                ["SIDE", "MARKET_FAMILY", "PERIOD", "SIDE_OR_LINE", "LANDED", "MATCHES", "rate"]
            ].rename(
                columns={
                    "SIDE": "Side",
                    "MARKET_FAMILY": "Market",
                    "PERIOD": "Period",
                    "SIDE_OR_LINE": "Pick",
                    "LANDED": "Landed",
                    "MATCHES": "Of",
                    "rate": "Rate",
                }
            ),
            hide_index=True,
            use_container_width=True,
            # `format="percent"`, NOT "%.0f%%". The value is a fraction
            # (0.60), and a printf format prints it literally -- %.0f of 0.60
            # rounds to "1%", so a 60% rate showed as 1%. The "percent" token
            # is the one that reads a 0-1 fraction as a percentage.
            column_config={
                "Rate": st.column_config.ProgressColumn(
                    "Rate", min_value=0.0, max_value=1.0, format="percent"
                )
            },
        )

st.divider()

# --- what punters actually backed ------------------------------------------
st.subheader("What punters actually backed here")
st.caption(
    "Every booking slip in the fetch that names this fixture, rolled up by pick: "
    "how many slips chose it, how many times those slips were copied, and at "
    "what price."
)
picks = query(
    f"""
    SELECT share_code, followed_times, market_name, outcome_name, odds,
           history_wins, history_matches, resolution
    FROM ANALYTICS.gold_slip_leg_history
    WHERE event_id = '{safe_event}'
    """,
    stable=played,
)
if picks.empty:
    st.info("No booking slip in the corpus names this fixture.")
else:
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
        .sort_values(["SLIPS", "COPIES"], ascending=False)
    )
    top = by_pick.iloc[0]
    m = st.columns(5)
    m[0].metric("Slips", f"{len(per_slip):,}", help="Distinct slips naming this fixture.")
    m[1].metric("Copied", f"{int(copies.sum()):,}", help="Copies across those slips.")
    m[2].metric("Median copies per slip", f"{copies.median():,.0f}")
    m[3].metric("Distinct picks", f"{len(by_pick)}")
    m[4].metric(
        "Most backed",
        f"{top.SLIPS / len(per_slip):.0%}",
        help=f"{top.PICK}: on {int(top.SLIPS)} of {len(per_slip)} slips.",
    )
    st.caption(f"Most backed pick: **{top.PICK}**.")

    by_pick["SHARE"] = by_pick.SLIPS / len(per_slip)
    by_pick["FORM"] = [
        f"{int(w)}/{int(n)}" if pd.notna(n) and n else "no history"
        for w, n in zip(by_pick.WINS, by_pick.MATCHES, strict=True)
    ]
    by_pick["RESULT"] = by_pick.RESULT.fillna("")
    st.dataframe(
        by_pick[["PICK", "SLIPS", "SHARE", "COPIES", "MEDIAN_ODDS", "FORM", "RESULT"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "PICK": "Pick",
            "SLIPS": st.column_config.NumberColumn("Slips", format="%d"),
            "SHARE": st.column_config.ProgressColumn(
                "Share of slips", min_value=0.0, max_value=1.0, format="percent"
            ),
            "COPIES": st.column_config.NumberColumn("Copies", format="%d"),
            "MEDIAN_ODDS": st.column_config.NumberColumn("Median odds", format="%.2f"),
            "FORM": "Last 10",
            "RESULT": "Result",
        },
    )
