"""Time-series shaping for the dashboard's charts. Pure pandas, no warehouse.

Two jobs, both kept out of the page files so they can be tested:

1. `reduce_steps` -- cut a price series to its ~10 largest moves. The charts
   were plotting every tick, up to 557 for one market, and the moves that
   matter were lost in a comb of one-rung reprices.
2. `arbitrage_series` -- the arbitrage a market offered at every moment, from
   every book's price history, so a surebet card can show where it stands NOW
   (or at kick-off) and not only the instant it was detected.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd

POINTS = 10

# The detector's freshness bar (MAX_LEG_SPREAD_SECONDS in the pipeline). A
# current arbitrage whose best prices were last changed further apart than this
# is shown with a warning rather than as a live price: FINDINGS 13g.
MAX_LEG_SPREAD_SECONDS = 300


def _reduce_one(y: np.ndarray, t: np.ndarray, points: int, keep_t: np.ndarray) -> np.ndarray:
    """Indices to keep for one step series, earliest first.

    Charts draw these as steps (a price holds until changed), so a dropped
    point is drawn at the level of the last KEPT point before it. The error of
    dropping point i is therefore |y[i] - y[last kept before i]|, and the
    greedy step adds the point whose absence misrepresents the chart most,
    until `points` are kept or nothing is misrepresented at all.

    First and last are always kept: the chart must start where the history
    starts and end on the latest price. So is the last point at or before each
    `keep_t` -- a marker for a detected opportunity must sit ON the line, not
    beside a level the reduction invented.
    """
    n = len(y)
    if n <= points:
        return np.arange(n)
    kept = np.zeros(n, dtype=bool)
    kept[[0, n - 1]] = True
    for moment in keep_t:
        at = np.searchsorted(t, moment, side="right") - 1
        if at >= 0:
            kept[at] = True

    while kept.sum() < points:
        # Level each point would be drawn at: the last kept value at or before it.
        last_kept = np.maximum.accumulate(np.where(kept, np.arange(n), 0))
        error = np.abs(y - y[last_kept])
        error[kept] = -1.0
        worst = int(error.argmax())
        if error[worst] <= 0:
            break
        kept[worst] = True
    return np.flatnonzero(kept)


def reduce_steps(
    df: pd.DataFrame,
    *,
    x: str,
    y: str,
    by: Sequence[str] = (),
    points: int = POINTS,
    keep_x: Iterable[pd.Timestamp] = (),
) -> pd.DataFrame:
    """At most ~`points` rows per series, keeping the largest moves.

    `by` names the columns that separate series (a book, an outcome). Points
    kept for `keep_x` can take a series slightly past `points`; that is
    deliberate, the markers depend on them.
    """
    if df.empty:
        return df
    ordered = df.sort_values(list(by) + [x])
    keep_t = np.sort(pd.to_datetime(pd.Series(list(keep_x)), utc=True).to_numpy())
    groups = ordered.groupby(list(by), sort=False) if by else [((), ordered)]
    parts = []
    for _, series in groups:
        t = pd.to_datetime(series[x], utc=True).to_numpy()
        idx = _reduce_one(series[y].to_numpy(dtype=float), t, points, keep_t)
        parts.append(series.iloc[idx])
    return pd.concat(parts)


def arbitrage_series(
    ticks: pd.DataFrame,
    outcomes: Iterable[str],
    until: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """The arbitrage a market offered at every price change.

    `ticks` has OUTCOME_ID, BOOKMAKER_NAME, ODDS, FIRE_TIME. After each moment's
    changes, every book's latest price stands; the best price per outcome across
    books gives `1 / sum(1 / best)`. Values below 1.0 are kept -- the point is to
    see a surebet open and close, not only the instant it was above 1.

    `outcomes` is the set the arbitrage is over (the detected signal's legs); a
    moment where any of them has no price at all yields no row. `until` cuts the
    history, e.g. at kick-off for a played match.

    SPREAD_SECONDS is the gap between the last-change times of the books
    supplying the best prices. A price is carried forward from its last change,
    because the tick store records changes only, so a book that stopped
    publishing looks unchanged rather than gone; the spread is what exposes that.
    """
    wanted = {str(o) for o in outcomes}
    columns = ["FIRE_TIME", "ARBITRAGE", "SPREAD_SECONDS"]
    if ticks.empty or not wanted:
        return pd.DataFrame(columns=columns)

    frame = ticks[ticks.OUTCOME_ID.astype(str).isin(wanted) & (ticks.ODDS > 1)].copy()
    frame["FIRE_TIME"] = pd.to_datetime(frame.FIRE_TIME, utc=True)
    if until is not None:
        frame = frame[frame.FIRE_TIME <= pd.Timestamp(until).tz_convert("UTC")]
    frame = frame.sort_values("FIRE_TIME")

    latest: dict[tuple[str, str], tuple[float, pd.Timestamp]] = {}
    rows = []
    for moment, batch in frame.groupby("FIRE_TIME", sort=True):
        for tick in batch.itertuples(index=False):
            latest[(str(tick.OUTCOME_ID), tick.BOOKMAKER_NAME)] = (float(tick.ODDS), moment)
        best: dict[str, tuple[float, pd.Timestamp]] = {}
        for (outcome, _book), (odds, changed) in latest.items():
            if outcome not in best or odds > best[outcome][0]:
                best[outcome] = (odds, changed)
        if set(best) != wanted:
            continue
        changed_at = [c for _, c in best.values()]
        rows.append(
            (
                moment,
                1.0 / sum(1.0 / odds for odds, _ in best.values()),
                int((max(changed_at) - min(changed_at)).total_seconds()),
            )
        )
    return pd.DataFrame(rows, columns=columns)


def latest_odds(
    ticks: pd.DataFrame, until: pd.Timestamp | None = None
) -> pd.DataFrame:
    """The last price per (outcome, book) at or before `until`."""
    if ticks.empty:
        return ticks
    frame = ticks.copy()
    frame["FIRE_TIME"] = pd.to_datetime(frame.FIRE_TIME, utc=True)
    if until is not None:
        frame = frame[frame.FIRE_TIME <= pd.Timestamp(until).tz_convert("UTC")]
    return (
        frame.sort_values("FIRE_TIME")
        .groupby(["OUTCOME_ID", "BOOKMAKER_NAME"], as_index=False)
        .last()
    )
