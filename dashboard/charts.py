"""Plotly figures shared by the dashboard pages.

Every historical line drawn here goes through `series.reduce_steps` first: each
series is cut to its ~10 largest moves, keeping the points under any marked
opportunity so a marker always sits on the line it annotates.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
from dashboard.common import BOOK_COLOURS, BOOK_DASH, BOOK_OUTLINE
from dashboard.series import MAX_LEG_SPREAD_SECONDS, POINTS, reduce_steps

DETECTED = "#c0392b"
STALE = "#9aa0a6"
LINE = "#1f6feb"


def _kickoff_line(figure: go.Figure, kickoff_at: Any) -> None:
    if pd.notna(kickoff_at):
        figure.add_vline(
            x=kickoff_at,
            line_dash="dash",
            line_color="#888",
            annotation_text="kick-off",
            annotation_position="top left",
        )


def arbitrage_chart(
    track: pd.DataFrame,
    detections: pd.DataFrame,
    kickoff_at: Any,
    now_label: str,
) -> go.Figure:
    """A tracked market's arbitrage over time, with every detection marked.

    `track`: OBSERVED_AT, ARBITRAGE, LEG_SPREAD_SECONDS. `detections`:
    DETECTED_AT, ARBITRAGE. Points whose legs were further apart than the
    freshness bar are drawn grey: the number is real arithmetic, but not a price
    anyone could have taken.
    """
    line = track.dropna(subset=["ARBITRAGE"])
    reduced = reduce_steps(
        line, x="OBSERVED_AT", y="ARBITRAGE", points=POINTS, keep_x=detections.DETECTED_AT
    )
    fresh = reduced.LEG_SPREAD_SECONDS.fillna(10**9) <= MAX_LEG_SPREAD_SECONDS

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=reduced.OBSERVED_AT,
            y=reduced.ARBITRAGE,
            mode="lines+markers",
            line={"shape": "hv", "color": LINE, "width": 2},
            marker={"color": [LINE if f else STALE for f in fresh], "size": 6},
            name="arbitrage",
            customdata=(reduced.LEG_SPREAD_SECONDS.fillna(0) / 60).round(1),
            hovertemplate="%{y:.4f}<br>legs %{customdata} min apart<extra></extra>",
        )
    )
    figure.add_hline(
        y=1.0,
        line_dash="dot",
        line_color="#888",
        annotation_text="1.0 — above is a surebet",
        annotation_position="bottom right",
    )
    figure.add_trace(
        go.Scatter(
            x=detections.DETECTED_AT,
            y=detections.ARBITRAGE,
            mode="markers",
            marker={"symbol": "diamond", "size": 12, "color": DETECTED},
            name="surebet detected",
            hovertemplate="detected %{y:.4f}<extra></extra>",
        )
    )
    if not line.empty:
        last = line.iloc[-1]
        figure.add_trace(
            go.Scatter(
                x=[last.OBSERVED_AT],
                y=[last.ARBITRAGE],
                mode="markers",
                marker={"symbol": "star", "size": 14, "color": "#111"},
                name=now_label,
                hovertemplate=f"{now_label} %{{y:.4f}}<extra></extra>",
            )
        )
    _kickoff_line(figure, kickoff_at)
    figure.update_layout(
        height=300,
        margin={"t": 20, "b": 0, "l": 0, "r": 0},
        legend={"orientation": "h", "y": -0.2},
        hovermode="closest",
    )
    return figure


def _book_line(figure: go.Figure, book: str, x: Any, y: Any, emphasis: bool, dim: bool) -> None:
    """One book's step line in its own colour. msport is yellow on black: a
    black line drawn first and a narrower yellow one over it."""
    width = 3.5 if emphasis else 2.0
    colour = BOOK_COLOURS.get(book, "#666")
    opacity = 0.5 if dim else 1.0
    if book in BOOK_OUTLINE:
        figure.add_trace(
            go.Scatter(
                x=x,
                y=y,
                mode="lines",
                line={"shape": "hv", "width": width + 2.5, "color": BOOK_OUTLINE[book]},
                opacity=opacity,
                hoverinfo="skip",
                showlegend=False,
            )
        )
    figure.add_trace(
        go.Scatter(
            x=x,
            y=y,
            mode="lines+markers",
            line={
                "shape": "hv",
                "width": width,
                "color": colour,
                "dash": BOOK_DASH.get(book, "solid"),
            },
            marker={"size": 5, "color": colour},
            opacity=opacity,
            name=book,
            hovertemplate=f"{book} %{{y:.2f}}<extra></extra>",
        )
    )


def odds_chart(
    ticks: pd.DataFrame,
    highlight_book: str | None,
    opportunities: pd.DataFrame,
    kickoff_at: Any,
    opportunity_label: str,
    x_end: Any,
) -> go.Figure:
    """One outcome's price at every book, with detected opportunities marked.

    `ticks`: BOOKMAKER_NAME, ODDS, FIRE_TIME. `opportunities`: DETECTED_AT,
    ODDS, EV, BOOKMAKER_NAME -- drawn where they happened, at the price taken.

    Every line is carried forward to `x_end` (now, or kick-off for a played
    match). The tick store records CHANGES only, so a price published once and
    left alone is a single point: drawn as a line it was invisible, and the
    "latest" star sat at its one change -- before the EV detected against that
    same standing price hours later. A price holds until changed, so the line
    holds too.
    """
    end = pd.Timestamp(x_end)
    reduced = reduce_steps(
        ticks,
        x="FIRE_TIME",
        y="ODDS",
        by=["BOOKMAKER_NAME"],
        points=POINTS,
        keep_x=opportunities.DETECTED_AT if not opportunities.empty else (),
    )
    figure = go.Figure()
    for book, series in reduced.groupby("BOOKMAKER_NAME", sort=True):
        series = series.sort_values("FIRE_TIME")
        x = list(pd.to_datetime(series.FIRE_TIME, utc=True))
        y = list(series.ODDS)
        if x and end > x[-1]:
            x.append(end)
            y.append(y[-1])
        _book_line(
            figure,
            book,
            x,
            y,
            emphasis=book == highlight_book,
            dim=highlight_book is not None and book != highlight_book,
        )
    if not opportunities.empty:
        figure.add_trace(
            go.Scatter(
                x=opportunities.DETECTED_AT,
                y=opportunities.ODDS,
                mode="markers",
                marker={
                    "symbol": "diamond",
                    "size": 13,
                    "color": DETECTED,
                    "line": {"width": 1, "color": "#fff"},
                },
                name=opportunity_label,
                customdata=opportunities[["EV", "BOOKMAKER_NAME"]].to_numpy(),
                hovertemplate="EV %{customdata[0]:.3f} at %{customdata[1]}"
                "<br>odds %{y:.2f}<extra></extra>",
            )
        )
    if highlight_book is not None:
        mine = ticks[ticks.BOOKMAKER_NAME == highlight_book].sort_values("FIRE_TIME")
        if not mine.empty:
            figure.add_trace(
                go.Scatter(
                    x=[max(end, pd.Timestamp(mine.FIRE_TIME.iloc[-1]))],
                    y=[mine.ODDS.iloc[-1]],
                    mode="markers",
                    marker={"symbol": "star", "size": 15, "color": "#111"},
                    name="last recorded odds",
                    hovertemplate="last recorded %{y:.2f}<extra></extra>",
                )
            )
    _kickoff_line(figure, kickoff_at)
    figure.update_layout(
        height=360,
        margin={"t": 20, "b": 0, "l": 0, "r": 0},
        legend={"orientation": "h", "y": -0.2},
        hovermode="closest",
    )
    return figure


def book_bars(frame: pd.DataFrame, value: str, label: str) -> go.Figure:
    """One bar per book, in the book's own colour. `frame`: BOOKMAKER_NAME, value."""
    books = list(frame.BOOKMAKER_NAME)
    figure = go.Figure(
        go.Bar(
            x=books,
            y=frame[value],
            marker={
                "color": [BOOK_COLOURS.get(b, "#666") for b in books],
                "line": {
                    "color": [BOOK_OUTLINE.get(b, "rgba(0,0,0,0)") for b in books],
                    "width": 2.5,
                },
            },
            hovertemplate="%{x}: %{y:,}<extra></extra>",
        )
    )
    figure.update_layout(height=300, margin={"t": 10, "b": 0, "l": 0, "r": 0}, yaxis_title=label)
    return figure


def efficiency_bars(frame: pd.DataFrame) -> go.Figure:
    """Mean overround per market, sorted, red below zero and blue above.

    `frame`: LABEL, MEAN_OVERROUND, SIGNALS. Below zero the best prices across
    books pay more than they take -- the surebet side.
    """
    ordered = frame.sort_values("MEAN_OVERROUND")
    figure = go.Figure(
        go.Bar(
            x=ordered.MEAN_OVERROUND,
            y=ordered.LABEL,
            orientation="h",
            marker={"color": ["#d62728" if v < 0 else "#1f77b4" for v in ordered.MEAN_OVERROUND]},
            customdata=ordered.SIGNALS,
            hovertemplate="%{y}<br>mean overround %{x:.2%}<br>%{customdata} signals<extra></extra>",
        )
    )
    figure.update_layout(
        height=max(300, 22 * len(ordered)),
        margin={"t": 10, "b": 0, "l": 0, "r": 0},
        xaxis={"tickformat": ".1%", "title": "mean overround after shopping every book"},
        yaxis={"categoryorder": "array", "categoryarray": list(ordered.LABEL)},
    )
    return figure
