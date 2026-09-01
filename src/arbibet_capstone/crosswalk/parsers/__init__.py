"""
Parser registry: maps bookmaker name to its parse function.

Each parser takes raw JSON (dict) and a market_mappings DataFrame,
and returns a list[Market].
"""
from typing import Callable, Dict, Any, List
import pandas as pd
import logging

from arbibet_capstone.crosswalk.models import Market
from arbibet_capstone.crosswalk.parsers.msport import parse_msport
from arbibet_capstone.crosswalk.parsers.sportybet import parse_sportybet
from arbibet_capstone.crosswalk.parsers.bet9ja import parse_bet9ja
from arbibet_capstone.crosswalk.parsers.livescorebet import parse_livescorebet
from arbibet_capstone.crosswalk.parsers.ilotbet import parse_ilotbet

logger = logging.getLogger(__name__)

# Type: (raw_json, market_mappings) -> list[Market]
ParserFn = Callable[[dict, pd.DataFrame], List[Market]]

PARSER_REGISTRY: Dict[str, ParserFn] = {
    "msport": parse_msport,
    "sportybet": parse_sportybet,
    "bet9ja": parse_bet9ja,
    "livescorebet": parse_livescorebet,
    "ilotbet": parse_ilotbet,
}


def parse_bookmaker(
    bookmaker: str,
    raw_data: Any,
    market_mappings: pd.DataFrame,
) -> List[Market]:
    """
    Parse raw API response into validated Market objects.

    An absent payload or an unregistered bookmaker yields an empty list --
    both are ordinary "nothing to parse here" outcomes. A failure *inside* a
    parser is not: it propagates, because a book that silently returns no
    markets is indistinguishable from a book that had none, and that
    ambiguity is what makes a broken crosswalk invisible.
    """
    if raw_data is None:
        return []

    parser = PARSER_REGISTRY.get(bookmaker)
    if parser is None:
        logger.warning(f"No parser registered for '{bookmaker}'")
        return []

    return parser(raw_data, market_mappings)
