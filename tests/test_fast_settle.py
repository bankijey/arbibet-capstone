"""Pass 1 of settlement: msport's result parsed, refused when unsafe, settled by the engine."""

from __future__ import annotations

from arbibet_capstone.fast_settle import (
    names_agree,
    parse_msport,
    period_scores,
    settle_demand,
    trusted,
)


def _payload(
    status="Ended", whole="2:1", sections='["1:0"]', home="Kallithea", away="Panserraikos"
):
    return {
        "bizCode": 10000,
        "data": {
            "eventMatchStatus": status,
            "scoreOfWholeMatch": whole,
            "scoreOfSection": sections,
            "homeTeam": home,
            "awayTeam": away,
        },
    }


def _row(family, side_or_line, period="match", basis="regular"):
    return {
        "market_family": family,
        "period": period,
        "time_basis": basis,
        "side_or_line": side_or_line,
    }


def test_msport_result_is_parsed_as_the_endpoint_returns_it():
    result = parse_msport(_payload())
    assert result.status == "ended" and result.full_time == (2, 1) and result.half_time == (1, 0)
    assert result.score_text() == "2:1 (HT 1:0)"
    assert (
        parse_msport(_payload(status="Not start", whole=None, sections=None)).status
        == "not_started"
    )
    assert (
        parse_msport(_payload(status="Cancelled", whole=None, sections=None)).status == "cancelled"
    )
    assert parse_msport(_payload(status="H2")).status == "live"
    assert parse_msport({"data": None}) is None and parse_msport({}) is None


def test_only_a_normal_time_ended_match_gets_period_scores():
    scores, why = period_scores(parse_msport(_payload()))
    assert why is None
    assert (scores.h1.home, scores.h1.away) == (1, 0)
    assert (scores.h2.home, scores.h2.away) == (1, 1)
    assert scores.reg == scores.full and scores.et is None and scores.pens is None
    # Extra time or anything unusual is left for API-Football.
    assert period_scores(parse_msport(_payload(sections='["1:0","1:1","2:1"]')))[0] is None
    assert period_scores(parse_msport(_payload(sections="[]")))[0] is None
    assert period_scores(parse_msport(_payload(whole="0:0", sections='["1:0"]')))[0] is None
    assert period_scores(parse_msport(_payload(status="H2")))[1] == "status is live"
    assert period_scores(parse_msport(_payload(whole=None)))[1] == "no final score"


def test_the_engine_settles_families_api_football_never_covered():
    result = parse_msport(_payload())  # 2:1, half-time 1:0
    demand = [
        _row("1x2", "home"),
        _row("1x2", "draw"),
        _row("total_goals", "over@2.5"),
        _row("total_goals", "under@5.5"),  # a line above the old ladder
        _row("btts", "yes"),
        _row("total_goals", "over@0.5", period="1h", basis="1h"),  # a half market
        _row("1x2", "home", period="1h", basis="1h"),
        _row("total_goals_home", "over@1.5"),
        _row("1x2", "home"),  # asked twice (a signal and a slip leg): settled once
    ]
    verdicts, refusals = settle_demand(result, demand)
    got = {(v["market_family"], v["period"], v["side_or_line"]): v["verdict"] for v in verdicts}
    assert got[("1x2", "match", "home")] == "won"
    assert got[("1x2", "match", "draw")] == "lost"
    assert got[("total_goals", "match", "over@2.5")] == "won"
    assert got[("total_goals", "match", "under@5.5")] == "won"
    assert got[("btts", "match", "yes")] == "won"
    assert got[("total_goals", "1h", "over@0.5")] == "won"
    assert got[("1x2", "1h", "home")] == "won"
    assert got[("total_goals_home", "match", "over@1.5")] == "won"
    assert len(verdicts) == 8 and not refusals


def test_what_the_engine_cannot_settle_is_counted_not_written():
    verdicts, refusals = settle_demand(parse_msport(_payload()), [_row("no_such_family", "yes")])
    assert verdicts == [] and sum(refusals.values()) == 1
    # A match that did not end settles nothing, and says why.
    verdicts, refusals = settle_demand(parse_msport(_payload(status="H2")), [_row("1x2", "home")])
    assert verdicts == [] and refusals == {"status is live": 1}


def test_the_score_is_only_ours_when_msport_names_our_teams():
    result = parse_msport(_payload(home="SFC Etar Veliko Tarnovo", away="PFC Chernomorets Burgas"))
    assert not names_agree(result, "Levski Sofia", "Ludogorets")
    assert names_agree(result, "Etar Veliko Tarnovo", "Chernomorets Burgas")
    assert not names_agree(parse_msport(_payload(home=None, away=None)), "A", "B")


def test_an_accepted_listing_vouches_for_an_alias_but_never_for_a_flagged_fixture():
    result = parse_msport(_payload(home="Chungnam Asan FC", away="Cheonan City FC"))
    fixture = ("Asan Mugunghwa", "Cheonan City")
    assert not trusted(result, *fixture)  # the names alone do not agree
    accepted = ("ok", "Chungnam Asan FC", "Cheonan City FC")
    assert trusted(result, *fixture, listings=[accepted])
    assert trusted(result, *fixture, listings=[("cleared", *accepted[1:])])
    # A listing still awaiting review vouches for nothing.
    assert not trusted(result, *fixture, listings=[("candidate", *accepted[1:])])
    assert not trusted(result, *fixture, listings=[("unverified", *accepted[1:])])
    # The result must name the listing that was accepted, not some third match.
    assert not trusted(result, *fixture, listings=[("ok", "Seoul E-Land", "Busan IPark")])
    # Any book's accepted listing will do: books share feeds, and names.
    others = [("unverified", "JBK", "FF Jaro II"), ("ok", "Seoul E-Land", "Busan IPark"), accepted]
    assert trusted(result, *fixture, listings=others)
    assert not trusted(result, *fixture, listings=others[:2])
    # Flagged as a wrong match: never, even when the names agree.
    assert not trusted(parse_msport(_payload()), "Kallithea", "Panserraikos", flagged=True)
    assert trusted(parse_msport(_payload()), "Kallithea", "Panserraikos")
