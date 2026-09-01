"""
Parser for SportyBet API responses.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List
import pandas as pd

from arbibet_capstone.crosswalk.models import Market, Outcome

berlin = ZoneInfo("Europe/Berlin")


def parse_sportybet(data: dict, market_mappings: pd.DataFrame) -> List[Market]:
    """Parse SportyBet raw JSON into a list of Market objects."""
    markets_raw = data.get("data", {}).get("markets", [])
    if not markets_raw:
        return []

    results = []
    for m in markets_raw:
        # Build market ID with specifier
        try:
            if len(m.get("specifier", "")) == 0:
                market_id = str(m["id"])
            else:
                market_id = str(m["id"]) + ";" + m["specifier"].split("=")[1]
        except Exception:
            market_id = str(m["id"])

        market_name = m.get("desc", m.get("name", None))

        outcomes = []
        for o in m.get("outcomes", []):
            if o.get("isActive") == 1:
                last_change = m.get("lastOddsChangeTime", None)
                if last_change:
                    ts = int(last_change / 1000)
                    last_change = (
                        datetime.fromtimestamp(ts, tz=timezone.utc)
                        .astimezone(berlin)
                        .isoformat()
                    )

                outcomes.append(
                    Outcome(
                        id=str(o["id"]),
                        name=o.get("desc"),
                        odds=float(o["odds"]),
                        p=float(o["probability"]),
                        last_change=last_change,
                    )
                )

        if outcomes:
            results.append(
                Market(market_id=market_id, market_name=market_name, outcomes=outcomes)
            )

    return results
