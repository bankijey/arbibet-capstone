"""The bookmaker market crosswalk: bet9ja and livescorebet market keys → betradar ids."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

_CSV = Path(__file__).parent / "assets" / "msports_matches.csv"


@lru_cache(maxsize=1)
def market_mappings() -> pd.DataFrame:
    """The crosswalk table. Cached: it is reference data, read once per process."""
    df = pd.read_csv(_CSV)
    df["livescorebet"] = df["livescorebet"].astype("Int64")
    return df