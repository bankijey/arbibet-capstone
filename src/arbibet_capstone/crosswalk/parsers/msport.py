"""
Parser for MSport API responses.
"""
import numpy as np
import pandas as pd
from typing import List

from arbibet_capstone.crosswalk.models import Market, Outcome


def parse_msport(data: dict, market_mappings: pd.DataFrame) -> List[Market]:
    """Parse MSport raw JSON into a list of Market objects."""
    markets_raw = data.get("data", {}).get("markets", [])
    if not markets_raw:
        return []

    results = []
    for m in markets_raw:
        # Build market ID with specifier
        try:
            if len(m.get("specifiers", "")) == 0:
                market_id = str(m["id"])
            else:
                market_id = str(m["id"]) + ";" + m["specifiers"].split("=")[1]
        except Exception:
            market_id = str(m["id"])

        market_name = m.get("desc", m.get("name", None))

        outcomes = []
        for o in m.get("outcomes", []):
            if o.get("isActive") == 1:
                outcomes.append(
                    Outcome(
                        id=str(o["id"]),
                        name=o.get("description"),
                        odds=float(o["odds"]),
                        p=float(o.get("probability", np.nan)) if o.get("probability") is not None else None,
                    )
                )

        if outcomes:
            results.append(
                Market(market_id=market_id, market_name=market_name, outcomes=outcomes)
            )

    return results
