from arbibet_capstone.slips import payload_hash


def _payload(odds: float, probability: float, followed: int = 10, pick: str = "Home") -> dict:
    return {
        "followedTimes": followed,
        "bettableBetSlip": [
            {
                "event": {"eventId": "sr:match:1"},
                "outcome": {"description": pick, "odds": odds, "probability": probability},
            }
        ],
    }


def test_leg_price_drift_does_not_change_the_hash() -> None:
    assert payload_hash(_payload(1.85, 0.52)) == payload_hash(_payload(1.90, 0.50))


def test_followers_and_picks_still_do() -> None:
    base = payload_hash(_payload(1.85, 0.52))
    assert base != payload_hash(_payload(1.85, 0.52, followed=11))
    assert base != payload_hash(_payload(1.85, 0.52, pick="Away"))


def test_the_payload_itself_is_not_modified() -> None:
    payload = _payload(1.85, 0.52)
    payload_hash(payload)
    assert payload["bettableBetSlip"][0]["outcome"]["odds"] == 1.85
