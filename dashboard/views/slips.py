"""Booking slips: the fixtures punters built them on, and each slip leg by leg.

Two sections, each collapsible:

* **Deep dives** -- the fixtures the most distinct slips name, ten still to be
  played as cards and twenty already started as a list, each opening its own
  page.
* **Popular slips, checked leg by leg** -- the most-copied slips, upcoming and
  played ranked separately, with every leg's live match status and, once
  settled, how it resolved.

Data: the 'slips' document the runner publishes to Supabase -- every slipped
fixture ranked by distinct slips, the most-copied slips with their verdicts,
and those slips' legs with live status and results.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from dashboard.common import document, frame, is_upcoming, kickoff

SLIP_COUNT = 10
UPCOMING_DIVES = 10

st.title("Betting slips")
st.caption(
    "Booking slips other people published and copied on msport, the fixtures "
    "they were built on, and how every leg is doing."
)

BET_LINK = st.column_config.LinkColumn(
    "Bet",
    display_text="open ↗",
    help="The match on msport. A first-time visitor sees its welcome page once; "
    "open the link again.",
)
_RESULT = {"won": "won ✓", "lost": "lost ✗", "half_win": "half won", "half_loss": "half lost"}


def _open_deep_dive(event_id: str) -> None:
    # In-app navigation, same tab. `st.switch_page` CLEARS the query string, so
    # the event id is handed over in session_state; the fixture page restores it
    # to the URL for sharing.
    st.session_state["deep_dive_event"] = event_id
    st.switch_page("views/fixture.py")


@st.fragment
def deep_dives() -> None:
    st.caption(
        "The fixtures the most booking slips were built on, each with its own "
        "page: how every book priced it, what both sides had been doing, and what "
        "punters backed. Ranked by how many DISTINCT slips name the fixture, so "
        "one hugely-copied slip cannot make its fixtures look widely backed. "
        "Upcoming and past are ranked separately: the most-slipped fixtures of all "
        "time are old ones, and would otherwise crowd out every match still to be "
        "played."
    )
    popular = frame(
        document("slips")["popular"],
        {
            "eventId": "EVENT_ID",
            "fixture": "FIXTURE",
            "tournament": "TOURNAMENT",
            "kickoffAt": "KICKOFF_AT",
            "slips": "SLIPS",
            "follows": "FOLLOWS",
        },
        times=("KICKOFF_AT",),
    )
    popular["UPCOMING"] = is_upcoming(popular.KICKOFF_AT)
    upcoming = popular[popular.UPCOMING].head(UPCOMING_DIVES)

    st.markdown(f"**Upcoming** · the {len(upcoming)} most-slipped matches still to be played")
    if upcoming.empty:
        st.info("None.")
    for start in range(0, len(upcoming), 5):
        row_cards = upcoming.iloc[start : start + 5]
        for column, (_, row) in zip(st.columns(5), row_cards.iterrows(), strict=False):
            with column, st.container(border=True):
                st.markdown(f"**{row.FIXTURE}**")
                st.caption(
                    f"{row.TOURNAMENT or '—'}  \n{kickoff(row.KICKOFF_AT)}  \n"
                    f"**{int(row.SLIPS)}** slips · {int(row.FOLLOWS or 0):,} copies"
                )
                if st.button("Deep dive →", key=f"up-{row.EVENT_ID}", use_container_width=True):
                    _open_deep_dive(row.EVENT_ID)

    _past_table(popular[~popular.UPCOMING])


def _past_table(past: pd.DataFrame) -> None:
    """Every played fixture punters built slips on, filterable, most-slipped first.

    Filters run over ALL past fixtures, and the table then shows the top of
    what matches. Selecting a row opens its deep dive; deep dives are kept for
    45 days after kick-off.
    """
    st.markdown("**Past** · matches already played, most-slipped first")
    if past.empty:
        st.info("None.")
        return
    past = past.assign(TOURNAMENT=past.TOURNAMENT.fillna("—"))
    f1, f2, f3, f4, f5 = st.columns([3, 3, 3, 1.5, 1.5])
    text = f1.text_input("Fixture contains", placeholder="team name", key="past-text")
    leagues = f2.multiselect("Competition", sorted(past.TOURNAMENT.unique()), key="past-league")
    kicked = pd.to_datetime(past.KICKOFF_AT)
    dates = f3.date_input(
        "Kick-off between",
        value=(kicked.min().date(), kicked.max().date()),
        min_value=kicked.min().date(),
        max_value=kicked.max().date(),
        key="past-dates",
    )
    min_slips = f4.number_input("Min slips", min_value=0, value=0, step=1, key="past-min")
    rows = f5.selectbox("Rows", [20, 50, 100], key="past-rows")

    mask = pd.Series(True, index=past.index)
    if text:
        mask &= past.FIXTURE.str.contains(text, case=False, na=False, regex=False)
    if leagues:
        mask &= past.TOURNAMENT.isin(leagues)
    if isinstance(dates, tuple) and len(dates) == 2:
        day = kicked.dt.date
        mask &= (day >= dates[0]) & (day <= dates[1])
    mask &= past.SLIPS >= min_slips
    matching = past[mask]
    shown = matching.head(rows).reset_index(drop=True)
    st.caption(
        f"Showing {len(shown)} of {len(matching)} matching fixtures. Select a row to open it."
    )

    picked = st.dataframe(
        shown[["FIXTURE", "TOURNAMENT", "KICKOFF_AT", "SLIPS", "FOLLOWS"]],
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        key="past-table",
        column_config={
            "FIXTURE": "Fixture",
            "TOURNAMENT": "Competition",
            "KICKOFF_AT": st.column_config.DatetimeColumn("Kick-off", format="D MMM YYYY, HH:mm"),
            "SLIPS": st.column_config.NumberColumn("Slips", format="%d"),
            "FOLLOWS": st.column_config.NumberColumn("Copies", format="%d"),
        },
    )
    selected = picked.selection.rows if picked else []
    if selected:
        _open_deep_dive(shown.iloc[selected[0]].EVENT_ID)


def _status(row: pd.Series) -> str:
    if pd.notna(row.MATCH_STATUS):
        status = str(row.MATCH_STATUS)
        if status == "Not start":
            return "not started"
        score = f" {row.SCORE}" if pd.notna(row.SCORE) and row.SCORE else ""
        if status == "Ended":
            return f"ended{score}"
        minute = f" · {row.PLAYED_TIME}" if pd.notna(row.PLAYED_TIME) and row.PLAYED_TIME else ""
        return f"live {status}{score}{minute}"
    return "not started" if row.UPCOMING else "kicked off"


def _slip_cards(cards: pd.DataFrame, legs: pd.DataFrame) -> None:
    for _, slip in cards.iterrows():
        rows = legs[legs.SHARE_CODE == slip.SHARE_CODE].copy()
        with st.container(border=True):
            head, body = st.columns([1, 3])
            head.metric("Copied by", f"{int(slip.FOLLOWED_TIMES or 0):,}")
            odds = slip.COMBINED_ODDS
            if pd.notna(odds):
                # Two decimals below 100, none above: rounding 1.19 to "1" once told
                # a reader a two-leg slip at even money was a long accumulator.
                head.metric(
                    "Combined odds",
                    f"{odds:,.2f}" if odds < 100 else f"{odds:,.0f}",
                    help="Product of every leg. What the slip pays if all of it lands.",
                )
            head.caption(
                f"`{slip.SHARE_CODE}` · {int(slip.LEGS)} legs · "
                f"{int(slip.LEGS_WITH_HISTORY)} with history"
            )
            to_play = int(rows.UPCOMING.sum()) if not rows.empty else 0
            total = int(slip.LEG_COUNT) if pd.notna(slip.LEG_COUNT) else len(rows)
            if rows.empty:
                head.badge("no legs recorded", color="grey")
            elif to_play == total:
                head.badge(
                    f"upcoming · first kick-off {kickoff(slip.FIRST_KICKOFF)}", color="green"
                )
            elif to_play:
                head.badge(f"part-played · {to_play} of {total} still to play", color="orange")
            else:
                head.badge("played · every leg has kicked off", color="grey")
            if int(slip.WON or 0) or int(slip.LOST or 0):
                head.caption(f"Settled so far: {int(slip.WON)} won · {int(slip.LOST)} lost")
            body.write(slip.SUMMARY)
            if rows.empty:
                continue
            rows["form"] = [
                f"{int(w)}/{int(m)}" if pd.notna(m) and m else "no history"
                for w, m in zip(rows.HISTORY_WINS, rows.HISTORY_MATCHES, strict=True)
            ]
            rows["fixture"] = rows.HOME_TEAM + " v " + rows.AWAY_TEAM
            rows["ko"] = rows.KICKOFF_AT.map(kickoff)
            rows["status"] = rows.apply(_status, axis=1)
            rows["result"] = rows.RESOLUTION.map(lambda r: _RESULT.get(r, r) if pd.notna(r) else "")
            with body.expander(f"{len(rows)} legs — the numbers behind this"):
                st.dataframe(
                    rows[
                        [
                            "fixture",
                            "ko",
                            "status",
                            "result",
                            "MARKET_NAME",
                            "OUTCOME_NAME",
                            "ODDS",
                            "form",
                            "URL",
                        ]
                    ].rename(
                        columns={
                            "fixture": "Fixture",
                            "ko": "Kick-off",
                            "status": "Status",
                            "result": "Result",
                            "MARKET_NAME": "Market",
                            "OUTCOME_NAME": "Pick",
                            "ODDS": "Odds",
                            "form": "Last 10",
                            "URL": "Bet",
                        }
                    ),
                    column_config={"Bet": BET_LINK},
                    hide_index=True,
                    use_container_width=True,
                )


@st.fragment
def popular_slips() -> None:
    st.caption(
        "Booking slips other people copied, each leg checked against how often "
        "that exact market has actually landed for the sides involved. The "
        "summary is written by gpt-4o-mini from the table beside it — the numbers "
        "are the model's input, shown so you can judge the output. Most legs land; "
        "the slip is all of them multiplied, and the price already says how often "
        "that happens. The Track record page shows the alternative construction."
    )
    st.caption(
        "**Upcoming** slips still have at least one fixture to play; a slip with "
        "some legs already kicked off stays here, marked part-played, until its "
        "last one starts. **Played** slips are the record. Each tab is ranked by "
        "copies on its own, because copies pile up over time and the most-copied "
        "slips of all time are always old ones. **Status** is the match as the "
        "books show it now; **Result** is how the leg settled."
    )
    # One row per slip: the newest verdict (QUALIFY -- a slip whose legs changed
    # gains a row, and the page must not show a stale card above its
    # replacement) beside the slip's leg count, kick-off span and results.
    body = document("slips")
    slips = frame(
        body["cards"],
        {
            "shareCode": "SHARE_CODE",
            "followedTimes": "FOLLOWED_TIMES",
            "legs": "LEGS",
            "legsWithHistory": "LEGS_WITH_HISTORY",
            "summary": "SUMMARY",
            "legCount": "LEG_COUNT",
            "firstKickoff": "FIRST_KICKOFF",
            "lastKickoff": "LAST_KICKOFF",
            "won": "WON",
            "lost": "LOST",
            "combinedOdds": "COMBINED_ODDS",
        },
        times=("FIRST_KICKOFF", "LAST_KICKOFF"),
    )
    waiting = body["waiting"]
    # Upcoming = its LAST leg has not kicked off yet, judged now (see is_upcoming).
    up_mask = is_upcoming(slips.LAST_KICKOFF)
    shown_up = slips[up_mask].head(SLIP_COUNT)
    shown_played = slips[~up_mask].head(SLIP_COUNT)
    codes = sorted(set(shown_up.SHARE_CODE) | set(shown_played.SHARE_CODE))
    leg_columns = {
        "eventId": "EVENT_ID",
        "home": "HOME_TEAM",
        "away": "AWAY_TEAM",
        "market": "MARKET_NAME",
        "pick": "OUTCOME_NAME",
        "odds": "ODDS",
        "kickoffAt": "KICKOFF_AT",
        "historyWins": "HISTORY_WINS",
        "historyMatches": "HISTORY_MATCHES",
        "resolution": "RESOLUTION",
        "url": "URL",
        "matchStatus": "MATCH_STATUS",
        "score": "SCORE",
        "playedTime": "PLAYED_TIME",
    }
    parts = [
        frame(body["legs"].get(code, []), leg_columns, times=("KICKOFF_AT",)).assign(
            SHARE_CODE=code
        )
        for code in codes
    ]
    legs = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if not legs.empty:
        legs["UPCOMING"] = is_upcoming(legs.KICKOFF_AT)

    slips_up, slips_played = st.tabs(["Popular upcoming", "Popular played"])
    with slips_up:
        if waiting:
            st.caption(
                f"{int(waiting)} upcoming slips have no verdict yet. Sixty are written "
                "every warm cycle, never-summarised and most-copied first."
            )
        if shown_up.empty:
            st.info("No upcoming slip has a verdict yet.")
        else:
            _slip_cards(shown_up, legs)
    with slips_played:
        if shown_played.empty:
            st.info("No played slips with a verdict.")
        else:
            _slip_cards(shown_played, legs)


with st.expander("Deep dives", expanded=True):
    deep_dives()

with st.expander("Popular slips, checked leg by leg", expanded=True):
    popular_slips()

st.divider()
st.caption(
    "**What these numbers are not.** `Last 10` is recent form over at most ten "
    "matches and ignores opponent strength — it is not a probability for the "
    "fixture in hand. Slips are the still-bettable remnant, not the original "
    "bet: legs vanish at kickoff. Every source behind this page rolls, so these "
    "figures are a reading, not a standing claim. Reference implementation, not "
    "a betting service."
)
