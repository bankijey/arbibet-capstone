"""The Telegram bot's pure parts: who is alerted, when, and what the messages say."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from runner.telegram import render
from runner.telegram.bot import Callbacks, parse_float
from runner.telegram.model import (
    Ledger,
    Leg,
    Opportunity,
    Subscriber,
    best_per_key,
    eligible,
    outcome_names,
    recipients,
    split_stake,
)
from runner.telegram.wallet import size_ev, size_surebet

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=3)


def surebet(
    value: float = 1.02,
    market: str = "18;2.5",
    kickoff: datetime = LATER,
    books: tuple[str, str] = ("bet9ja", "sportybet"),
) -> Opportunity:
    return Opportunity(
        kind="surebet",
        event_id="e1",
        market_id=market,
        fixture="Arsenal <U21> v Chelsea & Co",
        tournament="Premier League",
        kickoff=kickoff,
        market="Over/Under 2.5",
        value=value,
        legs=(
            Leg("over", books[0], 2.10, "https://a.example/x?y=1&z=2"),
            Leg("under", books[1], 2.05),
        ),
        detected_at=NOW,
        spread_seconds=42,
    )


def ev(value: float = 0.03, book: str = "msport", outcome: str = "1") -> Opportunity:
    return Opportunity(
        kind="ev",
        event_id="e1",
        market_id="1",
        fixture="A v B",
        tournament=None,
        kickoff=LATER,
        market="1x2",
        value=value,
        legs=(Leg("home", book, 2.2),),
        detected_at=NOW,
        probability=0.47,
        p_source="sportybet",
        outcome_id=outcome,
    )


# --- de-duplication ---------------------------------------------------------------------


def test_alerted_once_per_market_then_only_on_material_improvement():
    ledger = Ledger()
    first = surebet(1.020)
    assert ledger.due(7, first)
    ledger.mark(7, first)
    assert not ledger.due(7, surebet(1.020))
    assert not ledger.due(7, surebet(1.024))  # better, but not by the step
    assert ledger.due(7, surebet(1.025))
    # Another subscriber has its own record.
    assert ledger.due(8, surebet(1.020))


def test_a_fall_and_recovery_is_not_news():
    ledger = Ledger()
    ledger.mark(1, surebet(1.03))
    ledger.mark(1, surebet(1.01))  # marking lower never lowers the bar
    assert not ledger.due(1, surebet(1.03))


def test_ev_is_keyed_by_outcome_and_book():
    ledger = Ledger()
    ledger.mark(1, ev(0.03))
    assert not ledger.due(1, ev(0.04))
    assert ledger.due(1, ev(0.05))
    assert ledger.due(1, ev(0.03, book="bet9ja"))
    assert ledger.due(1, ev(0.03, outcome="2"))


def test_best_per_key_keeps_the_highest_combination():
    found = best_per_key([surebet(1.01), surebet(1.04), surebet(1.02), surebet(1.03, market="1")])
    assert [(o.market_id, o.value) for o in found] == [("18;2.5", 1.04), ("1", 1.03)]


# --- eligibility ------------------------------------------------------------------------


def test_never_after_kickoff_or_on_a_flagged_leg():
    assert eligible(surebet(), NOW, set(), 0.5)
    assert not eligible(surebet(kickoff=NOW), NOW, set(), 0.5)
    assert not eligible(surebet(), NOW, {("e1", "18;2.5", "sportybet")}, 0.5)
    # A flag on another market does not hide this one.
    assert eligible(surebet(), NOW, {("e1", "1", "sportybet")}, 0.5)


def test_only_true_cross_book_surebets_and_plausible_ev():
    assert not eligible(surebet(1.0), NOW, set(), 0.5)
    assert not eligible(surebet(books=("bet9ja", "bet9ja")), NOW, set(), 0.5)
    assert not eligible(ev(0.9), NOW, set(), 0.5)
    assert eligible(ev(0.02), NOW, set(), 0.5)


def test_subscriber_preferences():
    ledger = Ledger()
    people = [
        Subscriber(1),
        Subscriber(2, ev_min=0.05),
        Subscriber(3, surebets=False),
        Subscriber(4, muted_until=NOW + timedelta(hours=1)),
        Subscriber(5, active=False),
    ]
    assert [s.chat_id for s in recipients(surebet(), people, ledger, NOW)] == [1, 2]
    assert [s.chat_id for s in recipients(ev(0.03), people, ledger, NOW)] == [1, 3]
    ledger.mark(1, ev(0.03))
    assert [s.chat_id for s in recipients(ev(0.03), people, ledger, NOW)] == [3]


# --- arithmetic -------------------------------------------------------------------------


def test_stake_split_pays_the_same_whatever_wins():
    split = split_stake([2.10, 2.05], 100)
    returns = [s * o for s, o in zip(split["stakes"], [2.10, 2.05], strict=True)]
    assert sum(split["stakes"]) == pytest.approx(100)
    assert returns[0] == pytest.approx(returns[1])
    assert split["returns"] == pytest.approx(returns[0])
    assert split["arbitrage"] == pytest.approx(1 / (1 / 2.10 + 1 / 2.05))
    assert split_stake([2.1, 1.0], 100) is None
    assert split_stake([2.1, 2.0], 0) is None


def test_outcome_names_from_parsed_markets():
    market = SimpleNamespace(
        market_id="18;2.5",
        outcomes=[SimpleNamespace(id="12", name="Over 2.5"), SimpleNamespace(id="13", name=None)],
    )
    other = SimpleNamespace(market_id="18;2.5", outcomes=[SimpleNamespace(id="13", name="Under")])
    names = outcome_names({"a": [market], "b": [other]})
    assert names == {("18;2.5", "12"): "Over 2.5", ("18;2.5", "13"): "Under"}


# --- messages ---------------------------------------------------------------------------


def _balanced(text: str) -> bool:
    for tag in ("b", "i", "a", "code"):
        opened = len(re.findall(rf"<{tag}[ >]", text))
        if opened != text.count(f"</{tag}>"):
            return False
    return True


def test_surebet_alert_escapes_and_shows_the_split():
    sizing = size_surebet(
        [2.10, 2.05], ["bet9ja", "sportybet"], {"bet9ja": 1e6, "sportybet": 1e6}, 100
    )
    text = render.alert(surebet(), NOW, sizing)
    assert "SUREBET 1.0200" in text
    assert "Arsenal &lt;U21&gt; v Chelsea &amp; Co" in text
    assert 'href="https://a.example/x?y=1&amp;z=2"' in text
    assert "sportybet" in text and "stake <b>" in text
    assert "returns <b>" in text
    assert _balanced(text)


def test_ev_alert_shows_probability_and_fair_odds():
    text = render.alert(ev(0.034), NOW, size_ev(0.034, 2.2, "msport", {"msport": 50}, 50, 50))
    assert "EV +3.40%" in text
    assert "fair odds 2.13" in text
    assert _balanced(text)


def test_clip_stays_under_the_limit_and_cuts_at_a_line():
    text = "\n".join(f"<b>line {i}</b> " + "x" * 80 for i in range(200))
    clipped = render.clip(text)
    assert len(clipped) <= render.SAFE < render.LIMIT
    assert clipped.endswith("\n…")
    assert _balanced(clipped)


def _signals_doc() -> dict:
    def leg(key, outcome, book, odds, arb, detected, kickoff=LATER):
        return {
            "signalKey": key,
            "eventId": "e1",
            "marketId": "18;2.5",
            "outcomeId": outcome,
            "fixture": "A v B",
            "market": "Over/Under",
            "line": "2.5",
            "outcome": outcome,
            "book": book,
            "odds": odds,
            "url": None,
            "arbitrage": arb,
            "detectedAt": detected.isoformat(),
            "kickoffAt": kickoff.isoformat(),
            "offered": None,
            "currentOdds": None,
        }

    return {
        "arbitrage": {
            "legs": [
                leg("k1", "over", "bet9ja", 2.2, 1.05, NOW - timedelta(hours=2)),
                leg("k1", "under", "msport", 2.0, 1.05, NOW - timedelta(hours=2)),
                leg("k2", "over", "bet9ja", 2.1, 1.02, NOW - timedelta(hours=1)),
                leg("k2", "under", "sportybet", 2.05, 1.02, NOW - timedelta(hours=1)),
                {
                    **leg("k3", "over", "ilotbet", 2.5, 1.2, NOW, kickoff=NOW - timedelta(hours=1)),
                    "eventId": "e0",
                },
            ],
            "tracks": {
                "e1|18;2.5": {
                    "total": 3,
                    "last": {"at": NOW.isoformat(), "arbitrage": 0.99, "spreadSeconds": 0},
                    "points": [[NOW.isoformat(), 1.02, 0], [NOW.isoformat(), 0.99, 0]],
                }
            },
        },
        "ev": {
            "rows": [
                {
                    "eventId": "e1",
                    "marketId": "1",
                    "outcomeId": "1",
                    "fixture": "A v B",
                    "market": "1x2",
                    "line": None,
                    "outcome": "home",
                    "book": "msport",
                    "odds": 2.2,
                    "url": None,
                    "impliedP": 0.47,
                    "comparable": "sportybet",
                    "ev": ev_value,
                    "kickoffAt": LATER.isoformat(),
                    "isFresh": fresh,
                }
                for ev_value, fresh in ((0.034, True), (0.02, True), (0.05, False), (0.01, True))
            ],
            "prices": {},
        },
    }


def test_surebet_cards_use_the_best_and_the_newest_detection():
    cards = render.surebet_cards(_signals_doc(), set(), NOW)
    assert len(cards) == 1  # the kicked-off fixture is gone
    card = cards[0]
    assert card["best"] == 1.05 and card["detections"] == 2 and card["now"] == 0.99
    assert {leg["book"] for leg in card["legs"]} == {"bet9ja", "sportybet"}  # newest, k2
    text = render.surebet_card(card, 0, 1, 100, NOW)
    assert "Best detected <b>1.0500</b>" in text and "Now: <b>0.9900</b>" in text
    assert _balanced(text)


def test_a_flagged_leg_hides_its_detection():
    cards = render.surebet_cards(_signals_doc(), {("e1", "18;2.5", "sportybet")}, NOW)
    assert cards[0]["detections"] == 1
    assert {leg["book"] for leg in cards[0]["legs"]} == {"bet9ja", "msport"}


def test_ev_list_is_fresh_upcoming_and_above_threshold():
    rows = render.ev_rows(_signals_doc(), 0.015, set(), NOW)
    assert [r["ev"] for r in rows] == [0.034, 0.02]
    page = render.ev_page(rows, 0, 5, 0.015, NOW)
    assert "1–2 of 2" in page and _balanced(page)
    assert "No upcoming EV" in render.ev_page([], 0, 5, 0.1, NOW)


def test_track_summary_and_sparkline():
    text = render.track_summary(_signals_doc(), "e1", "18;2.5", "A v B")
    assert "First 1.0200" in text and "last 0.9900" in text
    assert render.spark([1, 2, 3]) == "▁▅█"
    assert render.reduce(list(range(100)), 5) == [0, 25, 50, 74, 99]


def test_slips_split_by_last_kickoff_and_render_legs():
    doc = {
        "cards": [
            {
                "shareCode": "UP",
                "followedTimes": 10,
                "lastKickoff": LATER.isoformat(),
                "combinedOdds": 5.0,
                "summary": "**Risky** <slip>",
            },
            {
                "shareCode": "DONE",
                "followedTimes": 99,
                "lastKickoff": NOW.isoformat(),
                "combinedOdds": 3.0,
                "won": 1,
                "lost": 1,
            },
        ],
        "legs": {
            "UP": [
                {
                    "home": "A",
                    "away": "B",
                    "market": "1X2",
                    "pick": "Home",
                    "odds": 1.5,
                    "kickoffAt": LATER.isoformat(),
                    "eventId": "e1",
                }
            ],
            "DONE": [
                {
                    "home": "C",
                    "away": "D",
                    "market": "1X2",
                    "pick": "Away",
                    "odds": 2.0,
                    "kickoffAt": NOW.isoformat(),
                    "resolution": "won",
                    "score": "0-1",
                    "historyWins": 3,
                    "historyMatches": 10,
                    "eventId": "e2",
                }
            ],
        },
    }
    upcoming, played = render.slip_lists(doc, NOW)
    assert [c["shareCode"] for c in upcoming] == ["UP"]
    assert [c["shareCode"] for c in played] == ["DONE"]
    text = render.slip(doc, upcoming[0], 0, 1, "Upcoming", NOW)
    assert "<b>Risky</b> &lt;slip&gt;" in text and "upcoming" in text
    played_text = render.slip(doc, played[0], 0, 1, "Played", NOW)
    assert "✅" in played_text and "history 3/10" in played_text
    assert _balanced(text) and _balanced(played_text)


def test_dive_sections_render_from_a_published_dive():
    dive = {
        "home": "AC Milan",
        "away": "Lecce",
        "tournament": "Serie A",
        "kickoffAt": LATER.isoformat(),
        "brief": {
            "summary": "**Tight** market",
            "model": "gpt-4o-mini",
            "generatedAt": NOW.isoformat(),
        },
        "result": None,
        "markets": [
            {
                "name": "1x2",
                "line": None,
                "books": 2,
                "ticks": 5,
                "series": {
                    "home": {
                        "bet9ja": [[NOW.isoformat(), 1.35], [NOW.isoformat(), 1.30]],
                        "msport": [[NOW.isoformat(), 1.32]],
                    }
                },
            }
        ],
        "teams": [
            {
                "name": "AC Milan",
                "role": "home",
                "matches": [
                    {
                        "result": "L",
                        "goalsFor": 0,
                        "goalsAgainst": 2,
                        "opponent": "Benfica",
                        "isHome": True,
                        "xg": 0.96,
                        "xga": 0.64,
                    }
                ],
            }
        ],
        "settled": [
            {
                "side": "AC Milan",
                "market": "total_goals",
                "pick": "over@0.5",
                "period": "match",
                "landed": 10,
                "of": 10,
            }
        ],
        "punters": {
            "slips": 4,
            "copies": 533,
            "medianCopies": 99,
            "picks": [
                {
                    "pick": "Over 1.5",
                    "slips": 2,
                    "copies": 369,
                    "medianOdds": 1.22,
                    "wins": 16,
                    "matches": 20,
                }
            ],
        },
    }
    assert "<b>Tight</b>" in render.dive_note(dive, "brief")
    assert "Not written yet" in render.dive_note(dive, "result")
    assert "best <b>1.32</b> msport" in render.dive_prices(dive)
    assert "L 0-2 v Benfica (H)" in render.dive_form(dive)
    assert "10/10" in render.dive_settled(dive)
    assert "history 16/20" in render.dive_punters(dive)
    for text in (render.dive_overview(dive, NOW), render.dive_prices(dive), render.dive_form(dive)):
        assert _balanced(text)


def test_fixture_search_puts_upcoming_first():
    doc = {
        "popular": [
            {
                "eventId": "1",
                "fixture": "Arsenal v X",
                "tournament": "PL",
                "follows": 5,
                "kickoffAt": NOW.isoformat(),
            },
            {
                "eventId": "2",
                "fixture": "Arsenal v Y",
                "tournament": "PL",
                "follows": 1,
                "kickoffAt": LATER.isoformat(),
            },
            {
                "eventId": "3",
                "fixture": "Z v W",
                "tournament": "Liga",
                "follows": 9,
                "kickoffAt": LATER.isoformat(),
            },
        ]
    }
    assert [p["eventId"] for p in render.fixtures(doc, NOW, "arsenal")] == ["2", "1"]
    assert [p["eventId"] for p in render.fixtures(doc, NOW)] == ["3", "2"]


def test_callback_tokens_fit_telegram_and_expire():
    callbacks = Callbacks(size=2)
    first = callbacks.put("surebet", 0)
    second = callbacks.put("flag", "e" * 36, "18;2.5", "sportybet", "A v B")
    third = callbacks.put("ev", 0.015, 5)
    assert all(len(t.encode()) <= 64 for t in (first, second, third))
    assert callbacks.get(first) is None
    assert callbacks.get(third) == ("ev", (0.015, 5))
    assert parse_float("2,05") == 2.05 and parse_float("x") is None


# --- runner scheduling and priority ----------------------------------------------------


def test_warm_cycles_start_on_a_fixed_clock():
    from runner.main import _next_warm, _slot

    slot = _slot(datetime(2026, 9, 17, 12, 7, 31, tzinfo=UTC))
    assert slot == datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    # Finished in time: the next slot, 15 minutes after this one STARTED.
    assert _next_warm(slot, slot + timedelta(minutes=9)) == (slot + timedelta(minutes=15), False)
    # Overran two slots: the next starts now, in the current slot, once.
    late = slot + timedelta(minutes=38)
    assert _next_warm(slot, late) == (slot + timedelta(minutes=30), True)


def test_warm_jobs_yield_to_the_hot_loop(monkeypatch):
    import threading
    import time

    from arbibet_capstone import priority

    monkeypatch.setattr(priority, "MIN_WORK_SECONDS", 0.0)
    priority.waited()
    assert priority.yield_to_hot() == 0.0

    release = threading.Event()

    def hot() -> None:
        with priority.hot_work():
            release.wait(1)

    thread = threading.Thread(target=hot)
    thread.start()
    time.sleep(0.05)
    threading.Timer(0.2, release.set).start()
    waited = priority.yield_to_hot(max_wait=2)
    thread.join()
    assert 0.1 < waited < 1.5
    assert priority.waited()["yields"] == 1
    # Never waits past its cap, however long the hot loop stays busy.
    with priority.hot_work():
        assert priority.yield_to_hot(max_wait=0.1) < 0.5
