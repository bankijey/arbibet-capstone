"""Backtest: every past positive-EV opportunity, bet by different strategies.

Pure pandas, no warehouse, so it is tested. The page hands it the settled
detections from `gold_ev_settled`.

TIMING -- which detection of an opportunity (fixture, market, outcome) to take:

    first   the first time the edge was seen
    best    the detection with the largest EV
    last    the last detection before kick-off (the closest to a closing price)

SIZING -- how much of the bankroll to stake:

    flat          1% of the STARTING bankroll on every bet, whatever happens
    fixed_2pct    2% of the current bankroll
    kelly         the Kelly fraction ev / (odds - 1), in full
    half_kelly    half of it
    quarter_kelly a quarter of it

Kelly is the growth-optimal stake IF the probability is right. Here the
probability is a bookmaker's (sportybet's or msport's), margin included, and
borrowed for the other books -- so full Kelly is expected to overbet, and the
fractional versions are the usual answer to an estimate you do not fully trust.
The backtest shows that rather than asserting it.

Mechanics. Bets are placed in time order at the chosen detection, staking from
CASH -- bankroll not already tied up in unsettled bets -- and each settles at
kick-off plus two hours. won pays stake x odds; lost pays nothing; push and void
return the stake; half_win and half_loss pay half of each.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

import pandas as pd

START = 100.0
SETTLE_AFTER = pd.Timedelta(hours=2)

TIMINGS = ("first", "best", "last")
SIZINGS = ("flat", "fixed_2pct", "kelly", "half_kelly", "quarter_kelly")

_OPPORTUNITY = ["EVENT_ID", "MARKET_ID", "OUTCOME_ID"]


def _kelly(ev: float, odds: float) -> float:
    return max(0.0, ev / (odds - 1)) if odds > 1 else 0.0


_STAKE: dict[str, Callable[[float, float, float], float]] = {
    # (bankroll, ev, odds) -> fraction of bankroll, except flat (absolute units)
    "flat": lambda _b, _e, _o: START * 0.01,
    "fixed_2pct": lambda b, _e, _o: b * 0.02,
    "kelly": lambda b, e, o: b * _kelly(e, o),
    "half_kelly": lambda b, e, o: b * _kelly(e, o) / 2,
    "quarter_kelly": lambda b, e, o: b * _kelly(e, o) / 4,
}


def _payout(verdict: str, stake: float, odds: float) -> float:
    return {
        "won": stake * odds,
        "lost": 0.0,
        "push": stake,
        "void": stake,
        "half_win": stake + stake * (odds - 1) / 2,
        "half_loss": stake / 2,
    }.get(verdict, stake)


def choose(detections: pd.DataFrame, timing: str) -> pd.DataFrame:
    """One settled detection per opportunity, picked by `timing`."""
    settled = detections[detections.VERDICT.notna()]
    if settled.empty:
        return settled
    if timing == "best":
        ordered = settled.sort_values(["EV", "DETECTED_AT"], ascending=[False, True])
    elif timing == "last":
        ordered = settled.sort_values("DETECTED_AT", ascending=False)
    else:
        ordered = settled.sort_values("DETECTED_AT")
    return ordered.drop_duplicates(_OPPORTUNITY, keep="first").sort_values("DETECTED_AT")


class Result(NamedTuple):
    curve: pd.DataFrame  # AT, BANKROLL -- after every settlement
    bets: int
    won: int
    staked: float
    profit: float
    final: float
    max_drawdown: float  # worst fall from a running peak, as a fraction


def simulate(bets: pd.DataFrame, sizing: str) -> Result:
    """Run one sizing strategy over `bets` (from `choose`)."""
    size = _STAKE[sizing]
    cash = START
    open_bets: list[tuple[pd.Timestamp, float]] = []  # (settles_at, payout)
    points: list[tuple[pd.Timestamp, float]] = []
    staked = 0.0
    won = 0

    def settle_until(moment: pd.Timestamp) -> None:
        nonlocal cash
        open_bets.sort(key=lambda b: b[0])
        while open_bets and open_bets[0][0] <= moment:
            at, payout = open_bets.pop(0)
            cash += payout
            points.append((at, cash + sum(p for _, p in open_bets)))

    placed = 0
    for bet in bets.itertuples(index=False):
        placed_at = pd.Timestamp(bet.DETECTED_AT)
        settle_until(placed_at)
        bankroll = cash + sum(p for _, p in open_bets)
        stake = min(cash, max(0.0, size(bankroll, float(bet.EV), float(bet.ODDS))))
        if stake <= 0:
            continue
        cash -= stake
        staked += stake
        placed += 1
        won += bet.VERDICT in ("won", "half_win")
        payout = _payout(str(bet.VERDICT), stake, float(bet.ODDS))
        open_bets.append((pd.Timestamp(bet.KICKOFF_AT) + SETTLE_AFTER, payout))
    settle_until(pd.Timestamp.max.tz_localize("UTC"))

    curve = pd.DataFrame(points, columns=["AT", "BANKROLL"])
    if curve.empty:
        return Result(curve, 0, 0, 0.0, 0.0, START, 0.0)
    running_peak = curve.BANKROLL.cummax().clip(lower=START)
    drawdown = float(((running_peak - curve.BANKROLL) / running_peak).max())
    final = float(curve.BANKROLL.iloc[-1])
    return Result(curve, placed, won, staked, final - START, final, drawdown)


def compare(detections: pd.DataFrame) -> pd.DataFrame:
    """Every timing x sizing, one row each."""
    rows = []
    for timing in TIMINGS:
        chosen = choose(detections, timing)
        for sizing in SIZINGS:
            r = simulate(chosen, sizing)
            rows.append(
                {
                    "timing": timing,
                    "sizing": sizing,
                    "bets": r.bets,
                    "hit_rate": r.won / r.bets if r.bets else None,
                    "staked": r.staked,
                    "profit": r.profit,
                    "roi": r.profit / r.staked if r.staked else None,
                    "final": r.final,
                    "max_drawdown": r.max_drawdown,
                }
            )
    return pd.DataFrame(rows)


# --- the paper wallet ------------------------------------------------------------------
#
# One bankroll, compounding, placed on EVERY signal the platform would have
# alerted: surebets of at least SUREBET_MIN, staked at SUREBET_FRACTION of the
# bankroll and split across their legs; and each positive-EV opportunity at a
# quarter of Kelly. A surebet's return is arithmetic -- stake x arbitrage
# whichever outcome lands -- so it needs no result, only that both legs were
# placed at the detected prices; it settles at kick-off plus two hours like
# everything else. The EV bets settle on the real verdict.

# In dollars: a bankroll a reader anywhere can picture, and small enough that
# the curve is about the edge, not the number of zeros.
PAPER_START = 1_000.0
SUREBET_MIN = 1.015
SUREBET_FRACTION = 0.20


class Wallet(NamedTuple):
    curve: pd.DataFrame  # AT, BANKROLL
    surebets: int
    ev_bets: int
    ev_won: int
    staked: float
    profit: float
    final: float
    max_drawdown: float
    open_bets: int  # placed, not yet settled at the end


def paper_wallet(
    surebets: pd.DataFrame, ev: pd.DataFrame, now: pd.Timestamp, start: float = PAPER_START
) -> Wallet:
    """`surebets`: one row per surebet (DETECTED_AT, KICKOFF_AT, ARBITRAGE), first
    detection per market. `ev`: settled EV detections as for `choose('first')`.
    `start`: the opening bankroll; every stake is a fraction of what it became."""
    events: list[tuple[pd.Timestamp, str, Any]] = [
        (pd.Timestamp(r.DETECTED_AT), "surebet", r) for r in surebets.itertuples(index=False)
    ] + [(pd.Timestamp(r.DETECTED_AT), "ev", r) for r in ev.itertuples(index=False)]
    events.sort(key=lambda e: e[0])

    cash = start
    open_bets: list[tuple[pd.Timestamp, float]] = []
    points: list[tuple[pd.Timestamp, float]] = []
    staked = 0.0
    n_sure = n_ev = ev_won = 0

    def settle_until(moment: pd.Timestamp) -> None:
        nonlocal cash
        open_bets.sort(key=lambda b: b[0])
        while open_bets and open_bets[0][0] <= moment:
            at, payout = open_bets.pop(0)
            cash += payout
            points.append((at, cash + sum(p for _, p in open_bets)))

    for at, kind, row in events:
        settle_until(at)
        bankroll = cash + sum(p for _, p in open_bets)
        if kind == "surebet":
            stake = min(cash, bankroll * SUREBET_FRACTION)
            payout = stake * float(row.ARBITRAGE)
            n_sure += 1
        else:
            stake = min(cash, bankroll * _kelly(float(row.EV), float(row.ODDS)) / 4)
            if stake <= 0:
                continue
            payout = _payout(str(row.VERDICT), stake, float(row.ODDS))
            n_ev += 1
            ev_won += row.VERDICT in ("won", "half_win")
        if stake <= 0:
            continue
        cash -= stake
        staked += stake
        open_bets.append((pd.Timestamp(row.KICKOFF_AT) + SETTLE_AFTER, payout))
    settle_until(now)
    still_open = len(open_bets)

    curve = pd.DataFrame(points, columns=["AT", "BANKROLL"])
    equity = cash + sum(p for _, p in open_bets)
    if curve.empty:
        return Wallet(curve, n_sure, n_ev, ev_won, staked, 0.0, start, 0.0, still_open)
    peak = curve.BANKROLL.cummax().clip(lower=start)
    drawdown = float(((peak - curve.BANKROLL) / peak).max())
    return Wallet(curve, n_sure, n_ev, ev_won, staked, equity - start, equity, drawdown, still_open)


def since(frame: pd.DataFrame, cutoff: pd.Timestamp | None) -> pd.DataFrame:
    """The bets detected at or after `cutoff` (None: all of them). The look-back
    window of the track record: the wallet then opens at the cutoff and takes
    only what was alerted from there on."""
    if cutoff is None or frame.empty:
        return frame
    return frame[pd.to_datetime(frame.DETECTED_AT, utc=True) >= cutoff]


def swings(curve: pd.DataFrame, start: float) -> pd.DataFrame:
    """The curve with the running PEAK, the DRAWDOWN from it (a fraction) and, per
    point, the CHANGE from the previous point -- what the swing chart colours."""
    out = curve.copy()
    if out.empty:
        return out.assign(PEAK=[], DRAWDOWN=[], CHANGE=[])
    out["PEAK"] = out.BANKROLL.cummax().clip(lower=start)
    out["DRAWDOWN"] = (out.PEAK - out.BANKROLL) / out.PEAK
    out["CHANGE"] = out.BANKROLL.diff().fillna(out.BANKROLL - start)
    return out
