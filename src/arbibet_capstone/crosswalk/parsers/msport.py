"""
Parser for MSport API responses.
"""
import numpy as np
import pandas as pd
from typing import List

from arbibet_capstone.crosswalk.models import Market, Outcome


def parse_msport(data: dict, market_mappings: pd.DataFrame) -> List[Market]:
    """Parse MSport raw JSON into a list of Market objects."""
    # `.get(k, {})` returns None when the key EXISTS with a null value, so the
    # default never fires. Books publish a well-formed 200 with a null body to
    # mean "I do not have this fixture" -- a known shape meaning no markets,
    # not an unknown shape. `or {}` keeps that distinction: this returns [],
    # while a genuinely unrecognised payload still raises.
    markets_raw = (data.get("data") or {}).get("markets") or []
    if not markets_raw:
        return []

    results = []
    for m in markets_raw:
        # A SUSPENDED market is not a price. msport flags it at market level
        # (`status` 1) while leaving each outcome's `isActive` at 1, so the
        # outcome check below never saw it. Measured over 24 hours of bronze:
        # 44,845 market snapshots at status 1, 89% of them in play, and the
        # same markets flipping 0 -> 1 -> 0 about 25,000 times a day -- the
        # suspend/resume pattern around a goal or a card. 4,896 were pre-match.
        # A signal on one is a bet the site will not take.
        if m.get("status", 0) != 0:
            continue

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
