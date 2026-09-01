"""
Parser for ILOTBet API responses.
"""
import pandas as pd
from typing import List

from arbibet_capstone.crosswalk.models import Market, Outcome


def parse_ilotbet(data: dict, market_mappings: pd.DataFrame) -> List[Market]:
    """Parse ILOTBet raw JSON into a list of Market objects."""
    markets_raw = data.get("data", {}).get("markets", [])
    if not markets_raw:
        return []

    results = []
    for m in markets_raw:
        # Build market ID with specifier alias
        specifier = m.get("specifiersAlias", "") or ""
        base_id = str(m.get("marketId", m.get("id", "")))
        market_id = f"{base_id};{specifier}" if specifier else base_id

        outcomes = []
        for o in m.get("odds", []):
            if o.get("active") != 1:
                continue
            outcomes.append(
                Outcome(
                    id=str(o.get("id", "")),
                    name=o.get("name"),
                    odds=float(o["odds"]),
                )
            )

        if outcomes:
            results.append(
                Market(market_id=market_id, market_name=None, outcomes=outcomes)
            )

    return results
