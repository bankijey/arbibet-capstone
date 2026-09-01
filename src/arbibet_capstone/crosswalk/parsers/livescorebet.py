"""
Parser for LiveScoreBet API responses.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List
import pandas as pd

from arbibet_capstone.crosswalk.models import Market, Outcome

berlin = ZoneInfo("Europe/Berlin")


def parse_livescorebet(data: dict, market_mappings: pd.DataFrame) -> List[Market]:
    """Parse LiveScoreBet raw JSON into a list of Market objects."""
    event_data = data.get("event", {})
    markets_raw = event_data.get("markets", [])
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

    # Group by marketId and build Market objects
    results = []
    for market_id_raw, group in lsb.groupby("marketId"):
        outcomes = []
        hcp_value = None

        for _, row in group.iterrows():
            sel = row["selections"]
            if not isinstance(sel, dict):
                continue

            outcome = _map_lsb_outcome(int(market_id_raw), sel)
            if outcome is not None:
                outcomes.append(outcome)

            # Track handicap for market ID suffix
            hcp = sel.get("hcp", None)
            if hcp:
                hcp_value = hcp

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
