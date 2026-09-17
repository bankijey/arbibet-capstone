from arbibet_capstone.summarise import Slip, SlipLeg, build_prompt, slips_from_rows


def _leg(**kwargs: object) -> SlipLeg:
    base = dict(
        fixture="Osnabruck v Bayern Munich",
        market="1X2",
        pick="Home",
        odds=38.89,
        implied_rate=0.026,
        historical_rate=0.5,
        wins=5,
        matches=10,
    )
    base.update(kwargs)
    return SlipLeg(**base)  # type: ignore[arg-type]


def test_a_leg_without_history_says_none_rather_than_a_number() -> None:
    # An absent record is a real answer. Rendering it as 0 would read as
    # "never happens", which is the opposite of "we do not know".
    slip = Slip("B2X", 100, [_leg(historical_rate=None, wins=None, matches=None)])

    body = build_prompt(slip)[1]["content"]

    assert "form: none" in body
    assert "0%" not in body


def test_form_is_shown_as_a_count_beside_the_rate() -> None:
    # 5/10 and 50% carry different information: the reader needs the sample
    # size to know how much the rate is worth.
    body = build_prompt(Slip("B2X", 100, [_leg()]))[1]["content"]

    assert "form: 5/10 (50%)" in body


def test_the_system_prompt_forbids_reading_form_as_a_probability() -> None:
    # The single most important instruction: a model handed a rate next to a
    # price will compare them as like quantities unless told not to.
    system = build_prompt(Slip("B2X", 1, [_leg()]))[0]["content"]

    assert "not a probability for this fixture" in system
    assert "Do not invent a record." in system


def test_the_prompt_leads_with_a_warehouse_computed_probability() -> None:
    # The combined probability is computed in SQL and handed to the model as
    # "1 in N" -- the model phrases it, never derives it. The opener must be
    # bold, and the figure must appear verbatim.
    slip = Slip(
        "B2X",
        5739,
        [_leg(), _leg()],
        combined_odds=411956.0,
        combined_probability=1 / 411956,
        one_in_n=411956,
    )

    messages = build_prompt(slip)
    system, user = messages[0]["content"], messages[1]["content"]

    assert "1 in 411,956" in user
    assert "bold" in system.lower()
    assert "do not compute or invent" in system.lower()
    # Must not read the popularity count as a probability -- the bug that put
    # "over 8,000 daily slips" into a verdict.
    assert "copied by" in system.lower()
    # Scale only: no invented external comparisons.
    assert "no lottery odds" in system.lower()


def test_the_signature_changes_when_a_leg_does() -> None:
    # A slip loses legs as matches kick off, so the same share_code names a
    # different bet over time and must earn a new summary.
    two = Slip("B2X", 1, [_leg(odds=2.0), _leg(odds=3.0)])
    one = Slip("B2X", 1, [_leg(odds=2.0)])

    assert two.signature() != one.signature()
    assert two.signature() == Slip("B2X", 1, [_leg(odds=2.0), _leg(odds=3.0)]).signature()


def test_rows_group_into_slips_by_share_code() -> None:
    rows = [
        {
            "SHARE_CODE": code,
            "FOLLOWED_TIMES": 5,
            "HOME_TEAM": "A",
            "AWAY_TEAM": "B",
            "MARKET_NAME": "1X2",
            "MARKET_FAMILY": "1x2",
            "OUTCOME_NAME": "Home",
            "SIDE_OR_LINE": "home",
            "ODDS": 1.5,
            "IMPLIED_RATE": 0.67,
            "HISTORICAL_RATE": 0.4,
            "WINS": 4,
            "MATCHES": 10,
        }
        for code in ("B2X", "B2X", "B2Y")
    ]

    slips = {s.share_code: s for s in slips_from_rows(rows)}

    assert len(slips["B2X"].legs) == 2
    assert len(slips["B2Y"].legs) == 1
    assert slips["B2X"].legs_with_history == 2


def test_short_slips_keep_their_decimals() -> None:
    # Rounding 1.19 to "1" told the model a two-leg slip at even money was a
    # long accumulator with a remote chance. It stated that confidently.
    slip = Slip("B2X", 1, [_leg(odds=1.17), _leg(odds=1.02)], combined_odds=1.19)
    body = build_prompt(slip)[1]["content"]

    assert "combined odds 1.19" in body


def test_long_accumulators_drop_the_decimals() -> None:
    slip = Slip("B2X", 1, [_leg(odds=11.0) for _ in range(4)], combined_odds=14641.0)
    body = build_prompt(slip)[1]["content"]

    assert "combined odds 14,641" in body


def test_the_signature_changes_when_only_the_evidence_does() -> None:
    # The regression this exists for. Slip B2RY7A6 sat on the dashboard saying
    # "recent form of 90%" beside a leg table reading "no history": its legs
    # never changed, but the fixtures stopped resolving to team ids, so the
    # form behind the sentence disappeared. The signature covered the prices
    # only, so the stale summary was treated as still current and never
    # rewritten. A summary is about its evidence, not just its legs.
    priced = _leg(odds=2.0)
    lost_its_history = _leg(odds=2.0, historical_rate=None, wins=None, matches=None)

    assert Slip("B2X", 1, [priced]).signature() != Slip("B2X", 1, [lost_its_history]).signature()


def test_a_likely_slip_is_not_forced_into_a_rarity_claim() -> None:
    # combined odds 1.19 gave one_in_n = round(1.19) = 1, and the model wrote
    # "1 in 1 ... extremely rare ... over 8,000 daily slips" for a slip that is
    # 84% likely. Below odds 2.0 the warehouse leaves one_in_n NULL, and the
    # opener must frame it as a LIKELY outcome instead.
    slip = Slip(
        "B2X",
        8462,
        [_leg(odds=1.17), _leg(odds=1.02)],
        combined_odds=1.19,
        combined_probability=1 / 1.19,
        one_in_n=None,
    )
    body = build_prompt(slip)[1]["content"]

    assert "LIKELY combined outcome" in body
    assert "1 in 1" not in body


def test_price_drift_does_not_change_what_a_verdict_is_about() -> None:
    # Slip payloads carry the legs' LIVE odds, which move on nearly every
    # fetch. When that counted as a change, half-hourly runs rewrote the same
    # most-copied slips and the upcoming backlog never moved.
    before = Slip(share_code="B2", followed_times=10, legs=[_leg(odds=1.85)])
    drifted = before._replace(legs=[_leg(odds=1.90)])

    assert before.signature() != drifted.signature()
    assert before.structure_signature() == drifted.structure_signature()


def test_a_changed_pick_or_form_is_a_new_verdict() -> None:
    before = Slip(share_code="B2", followed_times=10, legs=[_leg()])

    assert (
        before.structure_signature()
        != before._replace(legs=[_leg(pick="away")]).structure_signature()
    )
    assert (
        before.structure_signature()
        != before._replace(legs=[_leg(wins=9, matches=10)]).structure_signature()
    )
