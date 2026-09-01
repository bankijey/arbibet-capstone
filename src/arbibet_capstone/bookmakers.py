"""Bookmaker name normalisation between the bronze layer and the parsers.

`arbibet-markets` writes the bookmaker as `msport`; the parser registry ported
from `markets/parsers/` keys on `msports`. The mismatch fails silently in both
directions — an unregistered book yields no markets, and `fill_probabilities`
quietly loses one of its only two probability sources, which zeroes out EV
without raising anything — so the rename happens exactly once, here, at the
boundary where a bronze row becomes parser input.
"""

from __future__ import annotations

# Only books whose bronze name differs from their parser-registry name.
# Anything absent passes through unchanged.
_BRONZE_TO_PARSER: dict[str, str] = {
    "msport": "msports",
}


def to_parser_name(bronze_name: str) -> str:
    """Map a bronze `bookmaker` value to the name the parsers register under."""
    return _BRONZE_TO_PARSER.get(bronze_name, bronze_name)
