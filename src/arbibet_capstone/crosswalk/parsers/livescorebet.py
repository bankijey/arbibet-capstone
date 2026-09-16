"""
Parser for LiveScoreBet API responses.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List
import pandas as pd

from arbibet_capstone.crosswalk.models import Market, Outcome

berlin = ZoneInfo("Europe/Berlin")


def _canonical_line(row: "pd.Series") -> object:
    """The line this selection belongs to, in the HOME team's frame.

    Asian handicap (market 16) is published by livescorebet from each side's
    own perspective: a fixture at home -0.5 shows "Burnley -0.5" AND
    "Middlesbrough -0.5", where the away row means home +0.5. Every other book
    states both sides in the home frame ("Home (-0.5)" / "Away (+0.5)").

    Left un-negated, the away row lands in the wrong market and gets paired
    with the opposite side of a DIFFERENT line -- which reads as a ~30%
    arbitrage on a mainstream market at five books. The legacy implementation
    negated it; the refactor into `parsers/` lost that, and this restores it.
    """
    sel = row["selections"]
    if not isinstance(sel, dict):
        return None
    hcp = sel.get("hcp")
    if hcp in (None, ""):
        return None
    try:
        line = float(hcp)
    except (TypeError, ValueError):
        return hcp
    if int(row["marketId"]) == 16 and sel.get("outcomeType") == "AWAY":
        line = -line
    # One representation, or the two sides of a line group separately: a raw
    # string "-0.5" and a computed float -0.5 are different keys that format
    # into the same market id. `:g` keeps the minimal form (0.5 -> "0.5",
    # 1.0 -> "1"), which is what the betradar books publish.
    return f"{line:g}"


def parse_livescorebet(data: dict, market_mappings: pd.DataFrame) -> List[Market]:
    """Parse LiveScoreBet raw JSON into a list of Market objects."""
    # `.get(k, {})` returns None when the key EXISTS with a null value, so the
    # default never fires. Books publish a well-formed 200 with a null body to
    # mean "I do not have this fixture" -- a known shape meaning no markets,
    # not an unknown shape. `or {}` keeps that distinction: this returns [],
    # while a genuinely unrecognised payload still raises.
    event_data = data.get("event") or {}
    markets_raw = event_data.get("markets") or []
    if not markets_raw:
        return []

    lsb = pd.DataFrame(markets_raw)
    lsb["type"] = pd.to_numeric(lsb["type"], errors="coerce").astype("Int64")

    # Merge with mapping table
    mapping = market_mappings[["marketId", "livescorebet"]].dropna()
    mapping["livescorebet"] = mapping["livescorebet"].astype("Int64")
    lsb = lsb.merge(mapping, left_on="type", right_on="livescorebet", how="inner")
    lsb = lsb.explode("selections").reset_index(drop=True)
    lsb["marketId"] = lsb["marketId"].astype("Int64")

    # The handicap/line is part of the market's IDENTITY, so it has to be in
    # the grouping key. Grouping on marketId alone folds every Over/Under line
    # into one market and labels it with whichever line happened to come last:
    # Over 0.5 (1.02) and Over 6.5 (21.0) both become outcome `12` of market
    # `18;0.5`, and any best-price comparison then reports a ~7x arbitrage that
    # does not exist. `dropna=False` keeps markets that have no line at all --
    # 1X2, BTTS -- which pandas would otherwise drop silently.
    lsb["hcp"] = lsb.apply(_canonical_line, axis=1)

    results = []
    for (market_id_raw, hcp_value), group in lsb.groupby(["marketId", "hcp"], dropna=False):
        outcomes = []

        for _, row in group.iterrows():
            sel = row["selections"]
            if not isinstance(sel, dict):
                continue

            outcome = _map_lsb_outcome(int(market_id_raw), sel)
            if outcome is not None:
                outcomes.append(outcome)

        if not outcomes:
            continue

        market_id = f"{int(market_id_raw)};{hcp_value}" if hcp_value else str(int(market_id_raw))
        results.append(
            Market(market_id=market_id, market_name=None, outcomes=outcomes)
        )

    return results


def _map_lsb_outcome(market_id: int, sel: dict) -> Outcome | None:
    """Map a LiveScoreBet selection to a canonical Outcome."""
    outcome_type = sel.get("outcomeType", "")
    odds = sel.get("odds")
    if odds is None:
        return None

    canonical_id = None

    if market_id in [1, 60, 83]:
        mapping = {"HOME": "1", "TIE": "2", "AWAY": "3"}
        canonical_id = mapping.get(outcome_type)

    elif market_id in [18, 68, 90, 177]:
        mapping = {"OVER": "12", "UNDER": "13"}
        canonical_id = mapping.get(outcome_type)

    elif market_id == 14:
        mapping = {"HOME": "1711", "TIE": "1712", "AWAY": "1713"}
        canonical_id = mapping.get(outcome_type)

    elif market_id == 16:
        mapping = {"HOME": "1714", "AWAY": "1715"}
        canonical_id = mapping.get(outcome_type)

    elif market_id in [29, 31, 32, 34, 49, 51, 57, 75, 95]:
        mapping = {"YES": "74", "NO": "76"}
        canonical_id = mapping.get(outcome_type)

    elif market_id in [10, 63, 85]:
        mapping = {"1X": "9", "12": "10", "X2": "11"}
        canonical_id = mapping.get(outcome_type)

    elif market_id in [11, 64, 86]:
        mapping = {"HOME": "4", "AWAY": "5"}
        canonical_id = mapping.get(outcome_type)

    if canonical_id is None:
        return None

    # Parse last change timestamp
    last_change = None
    version = sel.get("version", "")
    if version and len(version) >= 14:
        try:
            ts = int(datetime.strptime(version[:-3], "%Y%m%d%H%M%S").timestamp())
            last_change = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(berlin).isoformat()
        except (ValueError, OSError):
            pass

    name = sel.get("name", sel.get("shortName", None))

    return Outcome(id=canonical_id, name=name, odds=float(odds), last_change=last_change)
