"""
Parser for Bet9ja API responses.
"""
import pandas as pd
from typing import List

from arbibet_capstone.crosswalk.models import Market, Outcome


def parse_bet9ja(data: dict, market_mappings: pd.DataFrame) -> List[Market]:
    """Parse Bet9ja raw JSON into a list of Market objects."""
    try:
        odds_raw = data["D"]["O"]
    except (KeyError, TypeError):
        return []

    # Flatten the odds dict into rows
    rows = []
    for k, v in odds_raw.items():
        parts = k.rsplit("_", 1)
        if len(parts) != 2:
            continue
        market_key, outcome_id = parts
        rows.append({
            "market_key": market_key,
            "outcomeId": outcome_id,
            "odds": float(v),
        })

    if not rows:
        return []

    b9odds = pd.DataFrame(rows)
    b9odds["specifier"] = b9odds["market_key"].apply(
        lambda x: x.split("@")[-1] if "@" in x else ""
    )
    b9odds["market_key"] = b9odds["market_key"].apply(
        lambda x: x.split("@")[0] if "@" in x else x
    )

    # Merge with market mappings
    mapping = market_mappings[["marketId", "bet9ja"]].dropna()
    b9odds = b9odds.merge(mapping, left_on="market_key", right_on="bet9ja", how="inner")
    b9odds = b9odds.drop(columns=["bet9ja"]).sort_values("marketId")

    # Group by marketId and build Market objects
    results = []
    for (market_id_raw, specifier), group in b9odds.groupby(["marketId", "specifier"]):
        outcomes = []
        for _, row in group.iterrows():
            outcome = _map_bet9ja_outcome(int(market_id_raw), row["outcomeId"], row["odds"], specifier)
            if outcome is not None:
                outcomes.append(outcome)

        if not outcomes:
            continue

        # Build final market_id string
        final_spec = specifier
        if int(market_id_raw) == 14 and specifier:
            try:
                s = int(specifier)
                final_spec = f"{s}:0" if s > 0 else f"0:{abs(s)}"
            except ValueError:
                pass

        market_id = f"{int(market_id_raw)};{final_spec}" if final_spec else str(int(market_id_raw))

        results.append(
            Market(market_id=market_id, market_name=None, outcomes=outcomes)
        )

    return results


def _map_bet9ja_outcome(market_id: int, outcome_id: str, odds: float, specifier: str) -> Outcome | None:
    """Map a Bet9ja outcome to a canonical outcome ID."""
    canonical_id = None
    name = outcome_id

    if market_id in [1, 60, 83]:
        mapping = {"1": "1", "X": "2", "2": "3"}
        canonical_id = mapping.get(outcome_id)

    elif market_id in [18, 68, 90, 177]:
        mapping = {"O": "12", "U": "13"}
        canonical_id = mapping.get(outcome_id)
        if canonical_id == "12":
            name = f"Over {specifier}"
        elif canonical_id == "13":
            name = f"Under {specifier}"

    elif market_id in [79, 544, 37]:
        mapping = {
            "1U2T": "794", "1U1T": "794", "1U": "794",
            "1O2T": "796", "1O1T": "796", "1O": "796",
            "XU2T": "798", "XU1T": "798", "XU": "798",
            "XO2T": "800", "XO1T": "800", "XO": "800",
            "2U2T": "802", "2U1T": "802", "2U": "802",
            "2O2T": "804", "2O1T": "804", "2O": "804",
        }
        canonical_id = mapping.get(outcome_id)

    elif market_id in [35, 78, 543]:
        mapping = {
            "1ANDGG": "78", "1HTANDGG": "78", "12HTANDGG": "78",
            "1ANDNG": "80", "1HTANDNG": "80", "12HTANDNG": "80",
            "XANDGG": "82", "XHTANDGG": "82", "X2HTANDGG": "82",
            "XANDNG": "84", "XHTANDNG": "84", "X2HTANDNG": "84",
            "2ANDGG": "86", "2HTANDGG": "86", "22HTANDGG": "86",
            "2ANDNG": "88", "2HTANDNG": "88", "22HTANDNG": "88",
        }
        canonical_id = mapping.get(outcome_id)

    elif market_id == 14:
        mapping = {"1H": "1711", "XH": "1712", "2H": "1713"}
        canonical_id = mapping.get(outcome_id)

    elif market_id == 16:
        mapping = {"1": "1714", "2": "1715"}
        canonical_id = mapping.get(outcome_id)

    elif market_id in [29, 31, 32, 34, 49, 51, 57, 75, 95]:
        mapping = {"Y": "74", "N": "76"}
        canonical_id = mapping.get(outcome_id)

    elif market_id in [10, 63, 85]:
        mapping = {"1X": "9", "12": "10", "X2": "11"}
        canonical_id = mapping.get(outcome_id)

    elif market_id in [542, 545, 546]:
        mapping = {
            "1XGGHT": "1718", "1XGG2T": "1718", "1XGG": "1718",
            "1XNGHT": "1719", "1XNG2T": "1719", "1XNG": "1719",
            "12GGHT": "1720", "12GG2T": "1720", "12GG": "1720",
            "12NGHT": "1721", "12NG2T": "1721", "12NG": "1721",
            "X2GGHT": "1722", "X2GG2T": "1722", "X2GG": "1722",
            "X2NGHT": "1723", "X2NG2T": "1723", "X2NG": "1723",
        }
        canonical_id = mapping.get(outcome_id)

    elif market_id in [11, 64, 86]:
        mapping = {"1": "4", "2": "5"}
        canonical_id = mapping.get(outcome_id)

    elif market_id == 13:
        mapping = {"1": "780", "X": "782"}
        canonical_id = mapping.get(outcome_id)

    if canonical_id is None:
        return None

    return Outcome(id=canonical_id, name=name, odds=odds)
