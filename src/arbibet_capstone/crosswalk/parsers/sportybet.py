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
        # sportybet publishes the SAME (id, specifier) more than once -- one
        # entry per betting product -- and only one of them is live. The stale
        # entries keep their last prices, so a suspended ladder can sit hours
        # out of date beside the current one. Observed on a single fixture:
        # `18/total=3.5` at both 1.51 (status 0) and 3.50 (status 1, last
        # changed 8.4 hours earlier), which manufactured a 30% "arbitrage"
        # against books quoting the live line.
        #
        # The outcome-level `isActive` check below does not catch this: the
        # outcomes of a suspended market are themselves flagged active.
        if m.get("status") not in (0, None):
            continue
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
