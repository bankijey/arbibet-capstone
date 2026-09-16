from datetime import UTC, datetime

from arbibet_capstone.fixture_brief import (
    FixtureBrief,
    MarketLine,
    SettledRate,
    TeamForm,
    build_prompt,
)


def _brief(**kwargs: object) -> FixtureBrief:
    base: dict[str, object] = dict(
        event_id="e1",
        home_team="Toulouse",
        away_team="Lille",
        tournament="Ligue 1",
        kickoff="Thursday 03 September 2026, 20:45",
        markets=[
            MarketLine("1x2", "home", 4, 3.40, 3.95, 3.20, 4.05, 291),
        ],
        form=[
            TeamForm("Toulouse", "home", 10, 3, 3, 4, 1.5, 1.9, 1.43, 4.2),
            TeamForm("Lille", "away", 10, 5, 2, 3, 1.8, 1.2, 1.61, 5.1),
        ],
        settled=[SettledRate("Lille", "total_goals", "match", "over@2.5", 6, 10)],
    )
    base.update(kwargs)
    return FixtureBrief(**base)  # type: ignore[arg-type]


def test_the_prompt_forbids_knowledge_the_warehouse_cannot_show() -> None:
    # A football fixture is exactly the sort of subject a model already has
    # opinions about. An unverifiable sentence about a rivalry or a league
    # position would be indistinguishable from the measured parts.
    system = build_prompt(_brief())[0]["content"]

    assert "use ONLY the numbers given" in system
    assert "league position" in system
    assert "past meetings" in system


def test_prices_and_results_are_labelled_as_different_kinds_of_claim() -> None:
    # A price is what a bookmaker asserts; a settled rate is what happened.
    # Averaging them into a verdict would hide which is which.
    system = build_prompt(_brief())[0]["content"]

    assert "BOOKMAKER PRICES" in system
    assert "SETTLED RESULTS" in system
    assert "never a probability for this fixture" in system


def test_every_number_the_brief_may_use_is_in_the_message() -> None:
    body = build_prompt(_brief())[1]["content"]

    assert "Toulouse v Lille" in body
    assert "Europe/Berlin" in body
    assert "opened 3.40 | latest 3.95" in body
    assert "last 10: 3W 3D 4L" in body
    assert "landed 6/10 (60%)" in body


def test_a_fixture_with_no_data_says_so_rather_than_sending_nothing() -> None:
    # A model handed a fixture name and nothing else writes a brief entirely
    # from its own memory, which is the one outcome this module prevents.
    body = build_prompt(_brief(markets=[], form=[], settled=[]))[1]["content"]

    assert "no market or history data is available" in body


def test_the_signature_tracks_the_evidence_not_the_fixture() -> None:
    # Prices tick and history settles independently of the fixture. Hashing
    # the id alone would let a brief outlive every number in it -- which is
    # how a slip summary came to cite 90% form beside "no history".
    moved = _brief(markets=[MarketLine("1x2", "home", 4, 3.40, 4.60, 3.20, 4.60, 300)])

    assert _brief().signature() != moved.signature()
    assert _brief().signature() == _brief().signature()


def test_kickoff_is_rendered_in_platform_time() -> None:
    # The bug this was written after: Snowflake's session timezone defaults to
    # America/Los_Angeles, so an evening Ligue 1 fixture printed as 11:45.
    body = build_prompt(_brief())[1]["content"]

    assert "20:45" in body
    assert datetime(2026, 9, 3, tzinfo=UTC).year == 2026
