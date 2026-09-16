"""Team-perspective re-expression of settled outcomes (task 0011a).

**One fixture-level settled verdict + one team, in; a team-perspective row (or
a named refusal) out.** No session, no config, no I/O, no fact write. The
`resolvers/` AST sweep (`tests/test_module_boundary.py`) covers this file by
glob and asserts it holds no `connect(...)` call.

`fact_team_market_result` (D15) is the form + H2H table:
`(team_key, event_key, event_start, is_home, opponent_team_key, outcome_key,
result)`. **The `outcome_key` on the row is the FIXTURE-LEVEL outcome_key from
`fact_settlement`, unchanged** — the re-expression this module does is on the
verdict alone. The reason is D6: the outcome_key resolves through
`dim_outcome -> dim_market` to `(market_family, period, side_or_line)`, and
that tuple is book-neutral but *fixture-independent* (D25(a)). Rewriting the
side to a synthetic "team-perspective side" would either mint a second
dim_outcome that only this table would ever key against (a private taxonomy
D12 exists to prevent), or fold identity into the fact (a text column on the
fact, which D6 forbids). The resolver therefore keeps the outcome_key intact
and re-expresses **only the verdict** — the row's `result` — so downstream
form/H2H queries filter on the fixture-level `(market_family, side_or_line)`
they already know and read the team-relative `result` directly.

## The design hazard, spelled out

Symmetric and directional markets re-express differently, **and the
difference cannot be inferred from the family name or a side token's
spelling**. The rule is declared in `FAMILY_CLASSIFICATION` below and every
directional family names, per canonical side, which team's row keeps the
fixture-level verdict and which team's row mirrors it. A family absent from
`FAMILY_CLASSIFICATION` is REFUSED with `family_not_classified` — silence
must not default to `symmetric`, the same rule as `unsettleable`-never-`0`
and `other`-is-a-claim-not-a-shrug.

## The four classifications

    symmetric           A fixture-level fact both teams observe identically
                        (`total_goals`, `btts`, `correct_score`, …). Both
                        teams' rows carry the fixture-level verdict unchanged.
    directional         A market whose side names a fixture side (1X2, DC,
                        DNB/HNB/ANB, handicap, asian_handicap, htft, combos
                        with a directional leg, multiscores' win buckets).
                        A per-family declared table names, per canonical
                        side, the *primary team* — the team the side favors.
                        The primary team's row keeps the fixture verdict;
                        the non-primary team's row **mirrors** the verdict.
    team_scoped_home    A market about the fixture's HOME team specifically
                        (`total_goals_home`, `clean_sheet_home`,
                        `win_to_nil_home`, …). Emitted for the home team
                        only — the away team gets a
                        `team_scoped_not_applicable` refusal (see the
                        opponent-row ruling below).
    team_scoped_away    Symmetric of the above for `*_away` families.
    not_team_relevant   Player-prop families (D13 v2, gated on the player
                        crosswalk). No team-perspective row exists;
                        refused with `family_not_team_relevant`.

## The mirror is on the verdict, not on the side

The fence spells out the reading: `home` means *win* to the home team and
*loss* to the away team. That is a **verdict** flip for the non-primary
team; the side stays as the fixture-level side. Concretely, `won <-> lost`
and (per D28's two new tokens, whose P&L signs are `+` and `-` respectively)
`half_win <-> half_loss`; `push`, `void` and `unsettleable` self-mirror
because their P&L is zero or absent, and flipping them would state a false
sign about the team's exposure. This is *consistent with D28(g)* — D28(g)
already ruled that a half-win counts as a `won` for form/H2H strike rate,
by P&L sign — and does not re-decide it: the mirror flips sign because a
team-perspective flip is exactly a P&L-sign flip.

The verdict-mirror rule keeps the *side* unchanged, which is what makes
form queries clean: for a directional family, filter on the side that names
this team's own perspective (`side_or_line = 'home'` for a 1X2 form query
that reads "did this team's own side win"), and the mirror on the away
team's row is what turns the row's `result` into a team-relative fact. See
the two acceptance queries at the bottom of this docstring for the
worked-out shape.

The **subtle failure the fence names** — inverting a form table by mirroring
the wrong thing — is what the "rows must differ" test set targets: every
directional family gets a fixture that produces a **DIFFERENT** verdict for
the two teams' rows (0009c's lesson, one layer over: a test where both
rows match proves nothing about a directional family). Push, void and
unsettleable inputs, on the other hand, produce IDENTICAL rows because the
mirror is a no-op on those tokens by design; those cases are also tested,
to pin the identity end of the rule.

## The opponent-row question for team-scoped families

D15 asks whether a `total_goals_home` produces one row (the home team's) or
two (the away team's row reading as a "goals conceded" fact). Both are
defensible; **the ruling here is one row** — the home team's — with a
`team_scoped_not_applicable` refusal for the away team.

The reason: `dim_market.market_family = 'total_goals_home'` is book-neutral
but names a specific fixture side. An away team's row keyed on that outcome
would filter into "form for team X on `total_goals_home`" queries whose
natural reading is "X's own goals" — but the row would silently mean X's
opponent's goals for half the fixtures. Downstream marts that pattern-match
family names would misread; forcing them to consult `is_home` on every
filter to know what the row *means* is exactly the "two vocabularies for
one concept" failure D15's own DDL note (0002/0003 reconciliation) refused
one grain up. The other perspective is preserved by the OPPONENT's OWN
`total_goals_away` fixture-level outcome (they own the `_away` family the
same way): a full form/H2H picture is reachable by UNIONing the two
matched families, without either row lying about its family.

## side_or_line, in 0005d's format

Parsed by `settlement.parse_side_or_line` — same rule, one grain over: the
side (a canonical token or a betradar structured id) optionally followed by
`@line[;line…]`. This resolver only ever inspects the *side* part; the line
values are irrelevant to the mirror (a handicap `home@-1.5` and a handicap
`home@-2.5` have the same mirror rule; the line only changes the fixture
verdict which is the calculator's job, not this one).

Combination families (`1x2_and_btts`, `double_chance_and_total_goals`,
`halftime_fulltime_and_*`, …) carry a `<leg1>_and_<leg2>` side. The
directional leg is always leg 1 (the leg the family name lists first),
which is where this resolver looks. Combinations pass all remaining checks
to the family's own primary-team table.

## The two D15 acceptance queries, concrete SQL

Written against 0003's `fact_team_market_result` grain and 0002's
`dim_outcome` / `dim_market`, so 0011b has a target rather than a
description. Both queries take the team_key as `:team_key` and produce
D15's example numbers when the fact tables are loaded.

Query (a): "Spurs over 2.5 in their last 5" — `total_goals` is symmetric,
so both teams' rows share the fixture-level verdict and a plain filter on
`side_or_line = 'over@2.5'` selects the right rows. Take the last five by
event_start (D15's serving-parameter FORM_WINDOW).

    SELECT r.event_start,
           r.event_key,
           r.result
      FROM fact_team_market_result r
      JOIN dim_outcome o ON o.outcome_key = r.outcome_key
      JOIN dim_market  m ON m.market_key  = o.market_key
     WHERE r.team_key    = :team_key
       AND m.market_family = 'total_goals'
       AND o.side_or_line  = 'over@2.5'
     ORDER BY r.event_start DESC
     LIMIT 5;

Query (b): "Chelsea won 6 of 7 at home" — `1x2` is directional; filter
`side_or_line = 'home'` reads as "this team's own side" because the
verdict-mirror rule flips `won`/`lost` for away-fixture rows. The `is_home`
predicate is the "at home" venue filter. D28(g)'s strike-rate rule
(`won`/`half_win` are wins, `lost`/`half_loss` are losses) is applied
verbatim so the vocabulary is honored without being re-decided.

    SELECT count(*) FILTER (WHERE r.result IN ('won', 'half_win')) AS wins,
           count(*) FILTER (WHERE r.result IN
               ('won', 'lost', 'half_win', 'half_loss'))            AS decided
      FROM fact_team_market_result r
      JOIN dim_outcome o ON o.outcome_key = r.outcome_key
      JOIN dim_market  m ON m.market_key  = o.market_key
     WHERE r.team_key      = :team_key
       AND r.is_home       = true
       AND m.market_family = '1x2'
       AND o.side_or_line  = 'home';

## What this resolver deliberately does NOT do

- It does not compute a settlement — that is `settlement.settle`.
- It does not read `dim_market` or `dim_outcome` — 0011b joins those.
- It does not write `fact_team_market_result` — 0011b does.
- It does not decide the `season` partition value — 0011b does, and its
  task spec calls out where it must come from without inferring it from
  `event_start` (D25(b)).
- It does not touch settlement's vocabulary, refusal reasons, or
  `calculator_version` — engine and both maps stay unmodified.
- It does not answer D28(g) a second time — the strike-rate rule the
  acceptance-query comment above applies is D28(g) verbatim.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from arbibet_capstone.crosswalk.settle.settlement import (
    VERDICT_HALF_LOST,
    VERDICT_HALF_WON,
    VERDICT_LOST,
    VERDICT_PUSH,
    VERDICT_UNSETTLEABLE,
    VERDICT_VOID,
    VERDICT_WON,
    VERDICTS,
    parse_side_or_line,
)

# --- which team, and a small vocabulary --------------------------------------

TEAM_HOME: Final = "home"
TEAM_AWAY: Final = "away"
TEAMS: Final = frozenset({TEAM_HOME, TEAM_AWAY})

#: A directional family's per-side declaration returns one of these tokens.
#: `both` means neither team gets the mirror (e.g. `draw` under 1X2, `push`
#: sides under DNB/HNB/ANB): the fixture verdict passes through for both
#: rows. `home`/`away` name the primary team whose row keeps the verdict;
#: the other team's row mirrors.
PRIMARY_HOME: Final = "home"
PRIMARY_AWAY: Final = "away"
PRIMARY_BOTH: Final = "both"

# --- classifications ---------------------------------------------------------

CLASS_SYMMETRIC: Final = "symmetric"
CLASS_DIRECTIONAL: Final = "directional"
CLASS_TEAM_SCOPED_HOME: Final = "team_scoped_home"
CLASS_TEAM_SCOPED_AWAY: Final = "team_scoped_away"
CLASS_NOT_TEAM_RELEVANT: Final = "not_team_relevant"

CLASSIFICATIONS: Final = (
    CLASS_SYMMETRIC,
    CLASS_DIRECTIONAL,
    CLASS_TEAM_SCOPED_HOME,
    CLASS_TEAM_SCOPED_AWAY,
    CLASS_NOT_TEAM_RELEVANT,
)

# --- refusal reasons ---------------------------------------------------------
#
# One per cause, the same discipline the settlement engine's
# `UNSETTLEABLE_REASONS` follows. A single "refused" counter would hide
# whether a hole in the classification table (a family we forgot) or a hole
# in a directional family's side table (a side we missed) is firing, and
# turn either into background noise behind whichever count dominates.

REASON_FAMILY_NOT_CLASSIFIED: Final = "family_not_classified"
REASON_FAMILY_NOT_TEAM_RELEVANT: Final = "family_not_team_relevant"
REASON_UNPARSEABLE_SIDE_OR_LINE: Final = "unparseable_side_or_line"
REASON_UNKNOWN_SIDE: Final = "unknown_side"
REASON_UNKNOWN_VERDICT: Final = "unknown_verdict"
REASON_UNKNOWN_TEAM: Final = "unknown_team"
REASON_TEAM_SCOPED_NOT_APPLICABLE: Final = "team_scoped_not_applicable"

REFUSAL_REASONS: Final = (
    REASON_FAMILY_NOT_CLASSIFIED,
    REASON_FAMILY_NOT_TEAM_RELEVANT,
    REASON_UNPARSEABLE_SIDE_OR_LINE,
    REASON_UNKNOWN_SIDE,
    REASON_UNKNOWN_VERDICT,
    REASON_UNKNOWN_TEAM,
    REASON_TEAM_SCOPED_NOT_APPLICABLE,
)


# --- output types ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TeamPerspectiveRow:
    """The team-perspective row for one (team, fixture-level outcome).

    The row's `outcome_key` on `fact_team_market_result` is the fixture-level
    outcome_key from `fact_settlement`, unchanged (see the module docstring
    for why the re-expression is on the verdict alone, not the side). This
    dataclass therefore carries only the re-expressed `result`.
    """

    result: str

    def __post_init__(self) -> None:
        if self.result not in VERDICTS:
            raise ValueError(f"unknown result token {self.result!r}")


@dataclass(frozen=True, slots=True)
class TeamPerspectiveRefusal:
    """No row is emitted for this (team, outcome). The reason is one of
    `REFUSAL_REASONS` and is what the loader (0011b) counts, kept apart the
    way the settlement engine keeps its `UNSETTLEABLE_REASONS` apart."""

    reason: str

    def __post_init__(self) -> None:
        if self.reason not in REFUSAL_REASONS:
            raise ValueError(f"unknown refusal reason {self.reason!r}")


# --- the verdict mirror -----------------------------------------------------
#
# won <-> lost and half_win <-> half_loss because their P&L signs are
# opposite (D28(g)). push / void / unsettleable self-mirror because their
# P&L is zero or absent — flipping any of them would state a false sign
# about the team's exposure. D28(g) is the ruling this table is consistent
# with; it is *not* re-decided here.

_MIRROR_VERDICT: Final[dict[str, str]] = {
    VERDICT_WON: VERDICT_LOST,
    VERDICT_LOST: VERDICT_WON,
    VERDICT_HALF_WON: VERDICT_HALF_LOST,
    VERDICT_HALF_LOST: VERDICT_HALF_WON,
    VERDICT_PUSH: VERDICT_PUSH,
    VERDICT_VOID: VERDICT_VOID,
    VERDICT_UNSETTLEABLE: VERDICT_UNSETTLEABLE,
}


def _mirror(verdict: str) -> str:
    return _MIRROR_VERDICT[verdict]


# --- per-family primary-team tables (directional families) ------------------
#
# Each table is the reviewable data the fence demands: a per-side mapping to
# the *primary team* (the team whose row keeps the fixture verdict). Missing
# a side here is `unknown_side`, never a silent default — the same rule the
# outcome map applies one grain over, so a new side that betradar adds to a
# family lands as a counted refusal, not a mis-mirrored row.

_1X2_PRIMARY: Final[dict[str, str]] = {
    "home": PRIMARY_HOME,
    "away": PRIMARY_AWAY,
    "draw": PRIMARY_BOTH,
}

_DC_PRIMARY: Final[dict[str, str]] = {
    "home_or_draw": PRIMARY_HOME,
    "draw_or_away": PRIMARY_AWAY,
    # `home_or_away` = "not a draw"; both teams read the same fact ("either
    # team won outright"), so both rows pass the verdict through unchanged.
    "home_or_away": PRIMARY_BOTH,
}

# draw_no_bet sides: home / away (draw pushes; the calculator returns a
# push verdict on a draw, which self-mirrors — both teams' rows are `push`).
_DNB_PRIMARY: Final[dict[str, str]] = {
    "home": PRIMARY_HOME,
    "away": PRIMARY_AWAY,
}

# home_no_bet sides: draw / away (home pushes).
_HNB_PRIMARY: Final[dict[str, str]] = {
    "draw": PRIMARY_BOTH,
    "away": PRIMARY_AWAY,
}

# away_no_bet sides: home / draw (away pushes).
_ANB_PRIMARY: Final[dict[str, str]] = {
    "home": PRIMARY_HOME,
    "draw": PRIMARY_BOTH,
}

# handicap sides: home / draw / away; the H:A line is irrelevant to the
# mirror — it changes the fixture verdict, not which team the side favors.
_HANDICAP_PRIMARY: Final[dict[str, str]] = {
    "home": PRIMARY_HOME,
    "draw": PRIMARY_BOTH,
    "away": PRIMARY_AWAY,
}

# asian_handicap sides: home / away (no draw side on asian handicaps).
_ASIAN_HANDICAP_PRIMARY: Final[dict[str, str]] = {
    "home": PRIMARY_HOME,
    "away": PRIMARY_AWAY,
}


def _htft_primary_from_ft(side: str) -> str | None:
    """halftime_fulltime side is `<ht>_<ft>`. The FT leg governs the
    primary team — the *final* score is what "who won this fixture" reads
    off, so a `home_away` (home led at HT, away won FT) is favorable to the
    away team, not the home team. The HT leg carries no independent primary
    signal for form purposes."""
    if "_" not in side:
        return None
    _ht, _sep, ft = side.partition("_")
    return {"home": PRIMARY_HOME, "away": PRIMARY_AWAY, "draw": PRIMARY_BOTH}.get(ft)


_MULTISCORES_ENUM_TOKENS: Final[dict[str, str]] = {
    "draw": PRIMARY_BOTH,
    "other_homewin": PRIMARY_HOME,
    "other_awaywin": PRIMARY_AWAY,
}


def _multiscores_primary(side: str) -> str | None:
    """multiscores' side is one of the enumerated tokens above OR a
    `_or_`-joined list of `H:A` scores. The enumerated buckets are grouped
    by direction (home-win scores vs away-win scores) in
    `SIDES_MULTISCORES`, so the first score's sign is the whole group's
    sign. Unknown token → `unknown_side` (returning None), not a silent
    default."""
    fixed = _MULTISCORES_ENUM_TOKENS.get(side)
    if fixed is not None:
        return fixed
    if "_or_" not in side and ":" not in side:
        return None
    parts = side.replace("_or_", "_").split("_")
    if not parts or ":" not in parts[0]:
        return None
    left, _, right = parts[0].partition(":")
    try:
        h, a = int(left), int(right)
    except ValueError:
        return None
    if h > a:
        return PRIMARY_HOME
    if a > h:
        return PRIMARY_AWAY
    return PRIMARY_BOTH


_COMBO_SEPARATOR: Final = "_and_"


def _split_combo(side: str) -> tuple[str, str] | None:
    """Same shape as `settlement._split_combo`: `_and_` rpartition so
    `home_or_draw_and_yes` splits cleanly at the combo boundary and the
    first leg keeps its own underscore. Re-defined locally so this
    resolver holds no import from settlement's *private* helpers — the
    module boundary is that the settlement engine is used through its
    public constants (`VERDICT_*`, `parse_side_or_line`, `SideAndLine`)
    only."""
    if _COMBO_SEPARATOR not in side:
        return None
    left, sep, right = side.rpartition(_COMBO_SEPARATOR)
    if not left or not right or sep != _COMBO_SEPARATOR:
        return None
    return (left, right)


def _combo_primary(side: str, leg_lookup: Callable[[str], str | None]) -> str | None:
    """For combination families, the *directional* leg is always leg 1
    (the leg the family name lists first: `1x2_and_btts`, `double_chance_
    and_total_goals`, `halftime_fulltime_and_*`). Split on `_and_` and run
    the leg's own primary lookup."""
    parts = _split_combo(side)
    if parts is None:
        return None
    return leg_lookup(parts[0])


def _dict_lookup(table: dict[str, str]) -> Callable[[str], str | None]:
    """A `.get`-shaped callable, typed so mypy accepts it as the
    `leg_lookup` argument of `_combo_primary`."""

    def lookup(side: str) -> str | None:
        return table.get(side)

    return lookup


# --- the classification table (the reviewable heart of this module) --------
#
# Every family the settlement engine can settle appears here, exactly once.
# A family absent from this table is REFUSED (`family_not_classified`); the
# same rule the outcome map applies for an unrecognised side. Adding a new
# family to the settlement engine's `TRANCHE_FAMILIES` without a row here
# means the resolver refuses it — a counted refusal, never a mis-classified
# row.
#
# `_test_every_settled_family_is_classified` in the test module pins the
# invariant so an edit to one file without the other fails visibly.

FAMILY_CLASSIFICATION: Final[dict[str, str]] = {
    # --- fixture-level facts both teams observe identically (symmetric) ---
    "total_goals": CLASS_SYMMETRIC,
    "btts": CLASS_SYMMETRIC,
    "correct_score": CLASS_SYMMETRIC,
    "odd_even_goals": CLASS_SYMMETRIC,
    "teams_to_score": CLASS_SYMMETRIC,
    "highest_scoring_half": CLASS_SYMMETRIC,
    "both_halves_over": CLASS_SYMMETRIC,
    "both_halves_under": CLASS_SYMMETRIC,
    "multigoals": CLASS_SYMMETRIC,
    "total_goals_and_btts": CLASS_SYMMETRIC,
    "halftime_fulltime_correct_score": CLASS_SYMMETRIC,
    "first_and_second_half_btts": CLASS_SYMMETRIC,
    "goal_bounds": CLASS_SYMMETRIC,
    "goal_bounds_first_half": CLASS_SYMMETRIC,
    # --- sides name a fixture side; verdict mirrors on the non-primary row ---
    "1x2": CLASS_DIRECTIONAL,
    "double_chance": CLASS_DIRECTIONAL,
    "draw_no_bet": CLASS_DIRECTIONAL,
    "home_no_bet": CLASS_DIRECTIONAL,
    "away_no_bet": CLASS_DIRECTIONAL,
    "handicap": CLASS_DIRECTIONAL,
    "asian_handicap": CLASS_DIRECTIONAL,
    "halftime_fulltime": CLASS_DIRECTIONAL,
    "1x2_and_btts": CLASS_DIRECTIONAL,
    "1x2_and_total_goals": CLASS_DIRECTIONAL,
    "double_chance_and_btts": CLASS_DIRECTIONAL,
    "double_chance_and_total_goals": CLASS_DIRECTIONAL,
    "halftime_fulltime_and_total_goals": CLASS_DIRECTIONAL,
    "halftime_fulltime_and_first_half_total_goals": CLASS_DIRECTIONAL,
    "halftime_fulltime_and_exact_goals": CLASS_DIRECTIONAL,
    "double_chance_and_first_half_btts": CLASS_DIRECTIONAL,
    "double_chance_and_second_half_btts": CLASS_DIRECTIONAL,
    "multiscores": CLASS_DIRECTIONAL,
    # --- markets about ONE fixture side; only that team's row is emitted ---
    "total_goals_home": CLASS_TEAM_SCOPED_HOME,
    "odd_even_home": CLASS_TEAM_SCOPED_HOME,
    "clean_sheet_home": CLASS_TEAM_SCOPED_HOME,
    "win_to_nil_home": CLASS_TEAM_SCOPED_HOME,
    "highest_scoring_half_home": CLASS_TEAM_SCOPED_HOME,
    "score_in_both_halves_home": CLASS_TEAM_SCOPED_HOME,
    "win_both_halves_home": CLASS_TEAM_SCOPED_HOME,
    "win_either_half_home": CLASS_TEAM_SCOPED_HOME,
    "multigoals_home": CLASS_TEAM_SCOPED_HOME,
    "goal_bounds_home": CLASS_TEAM_SCOPED_HOME,
    "total_goals_away": CLASS_TEAM_SCOPED_AWAY,
    "odd_even_away": CLASS_TEAM_SCOPED_AWAY,
    "clean_sheet_away": CLASS_TEAM_SCOPED_AWAY,
    "win_to_nil_away": CLASS_TEAM_SCOPED_AWAY,
    "highest_scoring_half_away": CLASS_TEAM_SCOPED_AWAY,
    "score_in_both_halves_away": CLASS_TEAM_SCOPED_AWAY,
    "win_both_halves_away": CLASS_TEAM_SCOPED_AWAY,
    "win_either_half_away": CLASS_TEAM_SCOPED_AWAY,
    "multigoals_away": CLASS_TEAM_SCOPED_AWAY,
    "goal_bounds_away": CLASS_TEAM_SCOPED_AWAY,
    # --- player-prop families (D13 v2) — not team-relevant ---
    "goalscorer_nth": CLASS_NOT_TEAM_RELEVANT,
    "goalscorer_last": CLASS_NOT_TEAM_RELEVANT,
    "goalscorer_anytime": CLASS_NOT_TEAM_RELEVANT,
    "player_to_score_two_plus": CLASS_NOT_TEAM_RELEVANT,
    "player_to_score_three_plus": CLASS_NOT_TEAM_RELEVANT,
    "player_fouls_won": CLASS_NOT_TEAM_RELEVANT,
    "player_not_to_score": CLASS_NOT_TEAM_RELEVANT,
}


# --- per-family primary lookup dispatch -------------------------------------


_DIRECTIONAL_LOOKUP: Final[dict[str, Callable[[str], str | None]]] = {
    "1x2": _dict_lookup(_1X2_PRIMARY),
    "double_chance": _dict_lookup(_DC_PRIMARY),
    "draw_no_bet": _dict_lookup(_DNB_PRIMARY),
    "home_no_bet": _dict_lookup(_HNB_PRIMARY),
    "away_no_bet": _dict_lookup(_ANB_PRIMARY),
    "handicap": _dict_lookup(_HANDICAP_PRIMARY),
    "asian_handicap": _dict_lookup(_ASIAN_HANDICAP_PRIMARY),
    "halftime_fulltime": _htft_primary_from_ft,
    "multiscores": _multiscores_primary,
    "1x2_and_btts": lambda s: _combo_primary(s, _dict_lookup(_1X2_PRIMARY)),
    "1x2_and_total_goals": lambda s: _combo_primary(s, _dict_lookup(_1X2_PRIMARY)),
    "double_chance_and_btts": lambda s: _combo_primary(s, _dict_lookup(_DC_PRIMARY)),
    "double_chance_and_total_goals": lambda s: _combo_primary(
        s, _dict_lookup(_DC_PRIMARY)
    ),
    "halftime_fulltime_and_total_goals": lambda s: _combo_primary(
        s, _htft_primary_from_ft
    ),
    "halftime_fulltime_and_first_half_total_goals": lambda s: _combo_primary(
        s, _htft_primary_from_ft
    ),
    "halftime_fulltime_and_exact_goals": lambda s: _combo_primary(
        s, _htft_primary_from_ft
    ),
    "double_chance_and_first_half_btts": lambda s: _combo_primary(
        s, _dict_lookup(_DC_PRIMARY)
    ),
    "double_chance_and_second_half_btts": lambda s: _combo_primary(
        s, _dict_lookup(_DC_PRIMARY)
    ),
}


# --- the entrypoint --------------------------------------------------------


def team_perspective(
    verdict: str,
    market_family: str,
    side_or_line: str,
    which_team: str,
) -> TeamPerspectiveRow | TeamPerspectiveRefusal:
    """Re-express one settled `(verdict, family, side_or_line)` from one
    team's perspective. Returns a `TeamPerspectiveRow` (with the possibly
    mirrored `result` token) or a `TeamPerspectiveRefusal` (with a named
    reason).

    The loader (0011b) iterates over `(event_key, outcome_key)` in
    `fact_settlement` and calls this once per team ∈ {home, away}. A row
    return is written; a refusal is counted per reason and no row is
    written.
    """
    if which_team not in TEAMS:
        return TeamPerspectiveRefusal(reason=REASON_UNKNOWN_TEAM)
    if verdict not in VERDICTS:
        return TeamPerspectiveRefusal(reason=REASON_UNKNOWN_VERDICT)

    classification = FAMILY_CLASSIFICATION.get(market_family)
    if classification is None:
        return TeamPerspectiveRefusal(reason=REASON_FAMILY_NOT_CLASSIFIED)

    if classification == CLASS_NOT_TEAM_RELEVANT:
        return TeamPerspectiveRefusal(reason=REASON_FAMILY_NOT_TEAM_RELEVANT)

    if classification == CLASS_TEAM_SCOPED_HOME:
        if which_team != TEAM_HOME:
            return TeamPerspectiveRefusal(reason=REASON_TEAM_SCOPED_NOT_APPLICABLE)
        return TeamPerspectiveRow(result=verdict)

    if classification == CLASS_TEAM_SCOPED_AWAY:
        if which_team != TEAM_AWAY:
            return TeamPerspectiveRefusal(reason=REASON_TEAM_SCOPED_NOT_APPLICABLE)
        return TeamPerspectiveRow(result=verdict)

    if classification == CLASS_SYMMETRIC:
        return TeamPerspectiveRow(result=verdict)

    # classification == CLASS_DIRECTIONAL: look the side up in the family's
    # primary table and either pass or mirror the verdict.
    parsed = parse_side_or_line(side_or_line)
    if parsed is None:
        return TeamPerspectiveRefusal(reason=REASON_UNPARSEABLE_SIDE_OR_LINE)

    lookup = _DIRECTIONAL_LOOKUP.get(market_family)
    # Unreachable while FAMILY_CLASSIFICATION and _DIRECTIONAL_LOOKUP are
    # in sync — the test suite pins the invariant. Kept as a positive
    # refusal, never a fabricated row, so an edit to one without the other
    # still lands as a counted refusal rather than a mis-mirrored row.
    if lookup is None:  # pragma: no cover
        return TeamPerspectiveRefusal(reason=REASON_FAMILY_NOT_CLASSIFIED)

    primary = lookup(parsed.side)
    if primary is None:
        return TeamPerspectiveRefusal(reason=REASON_UNKNOWN_SIDE)

    if primary == PRIMARY_BOTH:
        return TeamPerspectiveRow(result=verdict)
    if primary == which_team:
        return TeamPerspectiveRow(result=verdict)
    return TeamPerspectiveRow(result=_mirror(verdict))


__all__ = [
    "CLASSIFICATIONS",
    "CLASS_DIRECTIONAL",
    "CLASS_NOT_TEAM_RELEVANT",
    "CLASS_SYMMETRIC",
    "CLASS_TEAM_SCOPED_AWAY",
    "CLASS_TEAM_SCOPED_HOME",
    "FAMILY_CLASSIFICATION",
    "PRIMARY_AWAY",
    "PRIMARY_BOTH",
    "PRIMARY_HOME",
    "REASON_FAMILY_NOT_CLASSIFIED",
    "REASON_FAMILY_NOT_TEAM_RELEVANT",
    "REASON_TEAM_SCOPED_NOT_APPLICABLE",
    "REASON_UNKNOWN_SIDE",
    "REASON_UNKNOWN_TEAM",
    "REASON_UNKNOWN_VERDICT",
    "REASON_UNPARSEABLE_SIDE_OR_LINE",
    "REFUSAL_REASONS",
    "TEAM_AWAY",
    "TEAM_HOME",
    "TEAMS",
    "TeamPerspectiveRefusal",
    "TeamPerspectiveRow",
    "team_perspective",
]
