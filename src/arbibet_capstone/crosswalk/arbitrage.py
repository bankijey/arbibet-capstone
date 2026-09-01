"""
Arbitrage computation: combine outcomes, fill probabilities, detect opportunities.
"""
import logging
import time
from typing import List, Dict, Any

import numpy as np
import pandas as pd

from arbibet_capstone.crosswalk.models import Market

logger = logging.getLogger(__name__)


def markets_to_dataframe(bookmaker: str, markets: List[Market]) -> pd.DataFrame:
    """Convert validated Market objects into a flat DataFrame for computation."""
    rows = []
    for market in markets:
        for outcome in market.outcomes:
            rows.append({
                "bookmaker": bookmaker,
                "marketId": market.market_id,
                "marketName": market.market_name,
                "id": outcome.id,
                "name": outcome.name,
                "odds": outcome.odds,
                "p": outcome.p,
                "lastChange": outcome.last_change,
            })
    return pd.DataFrame(rows)


def combine_outcomes(parsed_data: Dict[str, List[Market]]) -> pd.DataFrame:
    """
    Combine parsed markets from all bookmakers into a single DataFrame.

    Parameters
    ----------
    parsed_data : dict mapping bookmaker name -> list[Market]

    Returns
    -------
    DataFrame with columns: bookmaker, marketId, marketName, id, name, odds, p, lastChange
    """
    frames = []
    for bookmaker, markets in parsed_data.items():
        if not markets:
            continue
        df = markets_to_dataframe(bookmaker, markets)
        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)

    # Deduplicate: keep most recent per (bookmaker, marketId) based on lastChange
    combined = (
        combined
        .sort_values(["marketId", "lastChange"], ascending=[True, False])
        .drop_duplicates(subset=["bookmaker", "marketId", "id"], keep="first")
        .reset_index(drop=True)
    )

    # Resolve marketName: pick the first non-null name per marketId
    names = (
        combined.dropna(subset="marketName")
        .drop_duplicates("marketId")[["marketId", "marketName"]]
    )
    combined = (
        combined.drop(columns="marketName")
        .merge(names, on="marketId", how="left")
    )

    # Clean up
    combined["id"] = combined["id"].apply(lambda x: str(x).strip() if x else None)
    combined = combined.dropna(subset=["id", "odds"], ignore_index=True)
    combined = combined.replace({np.nan: None})

    return combined


def fill_probabilities(outcomes: pd.DataFrame) -> pd.DataFrame:
    """
    Fill missing probabilities using priority: sportybet > msport.
    Computes EV = p * odds - 1.
    """
    if outcomes.empty:
        return outcomes

    outcomes = outcomes.copy()

    probs = (
        outcomes
        .query('bookmaker in ["msport", "sportybet"]')
        .sort_values(["bookmaker", "marketId", "id"])
        .drop_duplicates(["marketId", "id"])[["marketId", "id", "p"]]
    )

    if probs.empty:
        outcomes["EV"] = None
        return outcomes

    has_p = outcomes[outcomes["p"].notna()]
    missing_p = (
        outcomes[outcomes["p"].isna()]
        .drop(columns="p")
        .merge(probs, on=["marketId", "id"], how="left")
    )

    outcomes = pd.concat([has_p, missing_p]).sort_values(
        ["marketId", "id"], ignore_index=True
    )
    outcomes["p"] = pd.to_numeric(outcomes["p"], errors="coerce")
    outcomes["EV"] = outcomes["p"] * outcomes["odds"] - 1
    return outcomes


def assign_unique_bookmakers_for_market(market_df: pd.DataFrame) -> pd.DataFrame:
    """
    For a single market, assign one unique bookmaker per outcome to maximize arb.

    Rule: if a bookmaker appears for multiple outcomes, keep the one where it has
    the lowest odds (least valuable), and reassign the others to the next-best bookmaker.
    """
    df = market_df.copy()
    df = df.sort_values(["id", "odds"], ascending=[True, False])

    # Candidate list per outcome: (bookmaker, odds) sorted by odds desc
    candidates = (
        df.groupby("id")
        .apply(lambda g: list(zip(g["bookmaker"].tolist(), g["odds"].tolist())), include_groups=False)
        .to_dict()
    )

    # First pass: assign best bookmaker per outcome
    assignments = {}
    for outcome_id, cands in candidates.items():
        if not cands:
            continue
        bk, odd = cands[0]
        assignments[outcome_id] = {"bookmaker": bk, "odd": odd, "all_cands": cands}

    # Find bookmakers assigned to multiple outcomes
    by_bookmaker = {}
    for outcome_id, info in assignments.items():
        by_bookmaker.setdefault(info["bookmaker"], []).append((outcome_id, info["odd"]))

    to_reassign = set()
    used_bookmakers = set()

    for bk, lst in by_bookmaker.items():
        if len(lst) == 1:
            used_bookmakers.add(bk)
            continue
        # Keep the one with MIN odds, reassign the rest
        lst_sorted = sorted(lst, key=lambda t: t[1])
        used_bookmakers.add(bk)
        for outcome_id, _ in lst_sorted[1:]:
            to_reassign.add(outcome_id)

    # Second pass: reassign to next-best available bookmaker
    for outcome_id in to_reassign:
        cands = assignments[outcome_id]["all_cands"]
        new_bk, new_odd = None, None
        for bk, odd in cands:
            if bk not in used_bookmakers:
                new_bk, new_odd = bk, odd
                break
        if new_bk:
            used_bookmakers.add(new_bk)
            assignments[outcome_id]["bookmaker"] = new_bk
            assignments[outcome_id]["odd"] = new_odd

    # Build result
    out_rows = []
    for outcome_id, info in assignments.items():
        out_rows.append({
            "marketId": df["marketId"].iloc[0],
            "marketName": df["marketName"].iloc[0],
            "id": outcome_id,
            "bookmaker": info["bookmaker"],
            "odds": info["odd"],
        })

    return pd.DataFrame(out_rows)


def compute_arbitrage(parsed_data: Dict[str, List[Market]]) -> List[Dict[str, Any]]:
    """
    Full arbitrage pipeline:
    1. Combine outcomes from all bookmakers
    2. Fill missing probabilities
    3. Find markets with arb potential
    4. Assign unique bookmakers
    5. Return structured results

    Returns a list of dicts ready for JSON serialization into the DB.
    """
    t = time.time()
    outcomes = combine_outcomes(parsed_data)
    logger.info(f"    Combining outcomes took {time.time() - t:.3f}s")

    if outcomes.empty:
        logger.info("    No outcomes to process after combining.")
        return []

    t = time.time()
    outcomes = fill_probabilities(outcomes)
    logger.info(f"    Filling probabilities took {time.time() - t:.3f}s")

    # Detect positive EV bets
    if "EV" in outcomes.columns:
        evs = outcomes.query("EV >= 0.01 and p >= 0.5").drop_duplicates(["marketId", "id"])
        if not evs.empty:
            logger.info(f"    Found {len(evs)} positive EV bets.")

    # Filter to compactible markets (probability sums near 1, <=3 outcomes)
    t = time.time()
    market_stats = (
        outcomes
        .sort_values(["marketId", "bookmaker", "id"])
        .drop_duplicates(["marketId", "id"])
        .groupby("marketId")
        .agg({"p": "sum", "id": "count"})
        .query("p >= 0.95 and id <= 3")
        .index
    )
    compactible = outcomes.query("marketId in @market_stats")
    logger.info(f"    Filtering compactible markets took {time.time() - t:.3f}s")

    # Initial arb check: 1/sum(1/odds) >= 1
    t = time.time()
    arb_candidates = (
        compactible
        .groupby("marketId")["odds"]
        .apply(lambda x: None if len(x) < 2 else 1 / np.sum(1 / (x + 1e-9)))
        .reset_index(name="arbitrage")
        .query("arbitrage >= 1")
        .marketId
    )
    logger.info(f"    Initial arbitrage computation took {time.time() - t:.3f}s")

    # Build full outcomes payload (for all markets, not just arbs)
    outcomes_payload = (
        outcomes
        .groupby(["marketId", "marketName"], dropna=False)
        .apply(lambda g: g.to_dict("records"), include_groups=False)
        .reset_index(name="outcomes")
    )

    if len(arb_candidates) == 0:
        outcomes_payload["arbitrage"] = None
        outcomes_payload["results"] = None
        logger.info("    No arbitrage opportunities found.")
        return outcomes_payload.replace({np.nan: None}).to_dict("records")

    # Exclude known problematic markets
    excluded_ids = ["60010", "60011", "60012"]
    arb_ids = [mid for mid in arb_candidates if mid not in excluded_ids]

    # Assign unique bookmakers per market
    t = time.time()
    result_unique = (
        compactible.query("marketId in @arb_ids")
        .groupby("marketId", group_keys=False)[compactible.columns]
        .apply(assign_unique_bookmakers_for_market)
        .reset_index(drop=True)
    )
    logger.info(f"    Assigning unique bookmakers took {time.time() - t:.3f}s")

    # Compute final arbitrage values after unique assignment
    t = time.time()
    arbs = (
        result_unique
        .groupby("marketId")["odds"]
        .apply(lambda x: None if len(x) < 2 else 1 / np.sum(1 / (x + 1e-9)))
    )
    arbs.name = "arbitrage"

    merged = result_unique.merge(arbs, left_on="marketId", right_index=True, how="left")
    result = (
        merged
        .groupby(["marketId", "marketName", "arbitrage"], dropna=False)
        .apply(lambda g: g[["id", "bookmaker", "odds"]].to_dict("records"), include_groups=False)
        .reset_index(name="results")
        .sort_values("arbitrage", ascending=False)
    )
    logger.info(f"    Computing final arbitrage took {time.time() - t:.3f}s")

    # Merge arb results with full outcomes
    result_grouped = (
        result
        .groupby(["marketId", "arbitrage"], dropna=False)
        .apply(lambda g: g.to_dict("records"), include_groups=False)
        .reset_index(name="results")
    )

    payload = result_grouped.merge(outcomes_payload, on="marketId", how="right")
    payload = payload.replace({np.nan: None})

    return payload.to_dict("records")
