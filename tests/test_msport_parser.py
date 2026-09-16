from arbibet_capstone.crosswalk.parsers.msport import parse_msport


def _payload(status: int) -> dict:
    return {
        "data": {
            "markets": [
                {
                    "id": 18,
                    "specifiers": "total=2.5",
                    "status": status,
                    "outcomes": [
                        {"id": "12", "description": "Over 2.5", "odds": "1.90", "isActive": 1},
                        {"id": "13", "description": "Under 2.5", "odds": "1.90", "isActive": 1},
                    ],
                }
            ]
        }
    }


def test_an_open_market_is_parsed() -> None:
    (market,) = parse_msport(_payload(0), market_mappings=None)
    assert market.market_id == "18;2.5" and len(market.outcomes) == 2


def test_a_suspended_market_is_not_a_price_even_with_active_outcomes() -> None:
    # msport suspends at MARKET level and leaves every outcome isActive=1.
    assert parse_msport(_payload(1), market_mappings=None) == []
