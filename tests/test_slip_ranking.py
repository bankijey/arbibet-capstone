"""The slips page's order: live slips by what they have locked in, dead ones last."""

from __future__ import annotations

import pandas as pd
from publish.snapshot import rank_slips

NOW = pd.Timestamp("2026-09-21 12:00", tz="UTC")
LATER = "2026-09-22T18:00:00+00:00"
EARLIER = "2026-09-20T18:00:00+00:00"


def _card(code, won, lost, legs, copies, locked=1.0, last=LATER):
    return {
        "SHARE_CODE": code,
        "FOLLOWED_TIMES": copies,
        "LEGS": legs,
        "LEG_COUNT": legs,
        "LAST_KICKOFF": last,
        "WON": won,
        "LOST": lost,
        "LOCKED_IN": locked,
    }


def test_live_slips_rank_by_locked_in_then_dead_ones_then_played():
    cards = pd.DataFrame(
        [
            _card("dead-popular", won=6, lost=1, legs=8, copies=9000, locked=40.0),
            _card("untouched", won=0, lost=0, legs=3, copies=500),
            _card("two-won", won=2, lost=0, legs=3, copies=100, locked=3.8),
            _card("one-won-many-left", won=1, lost=0, legs=6, copies=800, locked=1.9),
            _card("played", won=4, lost=0, legs=4, copies=99999, locked=12.0, last=EARLIER),
            _card("untouched-copied", won=0, lost=0, legs=3, copies=700),
        ]
    )
    ranked = rank_slips(cards, NOW)
    assert list(ranked.SHARE_CODE) == [
        "two-won",
        "one-won-many-left",
        "untouched-copied",
        "untouched",
        "dead-popular",
        "played",
    ]
    assert list(ranked.RANK) == [1, 2, 3, 4, 5, 6]
    by_code = ranked.set_index("SHARE_CODE")
    assert bool(by_code.ALIVE["dead-popular"]) is False and bool(by_code.ALIVE["two-won"]) is True
    assert int(by_code.REMAINING["two-won"]) == 1 and int(by_code.REMAINING["dead-popular"]) == 1


def test_ranking_survives_missing_counts():
    cards = pd.DataFrame([_card("a", None, None, None, None)])
    cards["LEGS"] = [2]
    ranked = rank_slips(cards, NOW)
    assert bool(ranked.ALIVE.iloc[0]) and int(ranked.REMAINING.iloc[0]) == 2
