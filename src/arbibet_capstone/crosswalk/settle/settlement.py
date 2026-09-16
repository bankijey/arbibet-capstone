"""The settlement engine (pure): score + market -> verdict (task 0009a).

**Bytes of fact in, a verdict out.** No database session, no config, no I/O, no
bronze read, no fact write, and — the load-bearing constraint (D10) — *no
price in any signature*. Every `resolvers/` module already holds this line
(`tests/test_module_boundary.py`'s AST sweep) and this one does too, because
odds-freeness is what lets one settlement pass feed CLV, calibration and
"the price at T-24h" as downstream joins rather than as three parallel
settlement passes each pinned to a different odds snapshot.

CLAUDE.md's read order names "the reference settlement calculator (score/
market -> won/lost/void), ported and corrected per D18/D19". **The reference
is `legacy/market_calculator_fixed.py` in this repo — an untracked,
un-gitignored working-tree file this module's first pass failed to locate
and honestly said so.** Located post-commit; this docstring is the honest
citation, and every family this engine settles matches the reference's
`_1x2_result`/`_dc`/`_dnb`/`_yes_no`/`_correct_score`/`_exact_goals`/
`_goal_range`/`_handicap`/`_asian_hcp`/`_htft` pair-for-pair on the score/
result families where their vocabularies overlap.

The **three corrections the reference could not encode** are what CLAUDE.md
means by "*and corrected per D18/D19*", and are the whole reason silver's
architecture holds ids-first, basis-explicit, five-token instead of the
reference's label-parsing, single-`scores`, `None`-conflated shape:

1. **Time basis** — the reference reads a bare `scores` (whatever the raw
   feed happens to hold at that grain), so the AET final settles the same
   value on every market. D18 exists because that is silently wrong for
   knockouts past 90'; this engine reads `dim_market.time_basis` and the
   AET test proves the divergence.
2. **Identity by ids, not labels** — the reference parses `(0:1)` and
   `(-1.5)` out of `outcome_name` and matches `"home"` in the label text
   (D12 exactly the thing that layer is abolishing). This engine reads
   canonical sides only; the identity path never re-enters the label.
3. **One `None` for four different causes** — the reference collapses
   "unregistered market", "push", "malformed outcome", and "exception" into
   a single `None`. D10's five-token vocabulary and nine
   `UNSETTLEABLE_REASONS` here exist to keep those apart, and the mutation
   test would fail if any refusal regressed to `lost`.

The open case the reference exposed and D10's original five tokens could
not express — the asian-quarter mixed case, filed as
`specs/implementor/notes/0009a-settlement-engine.md` — is now **closed by
D28**: two new tokens (`half_win`, `half_loss`) join the vocabulary and
`_settle_asian_handicap` returns them directly on the mixed case,
replacing 0009a's documented interim collapse to `push`. D28 also widened
`fact_settlement.result_value` from `NUMERIC(2, 1)` to `NUMERIC(3, 2)`
in the same forward-only migration, because the pre-widening scale would
have silently rounded `0.75` to `0.8` and `0.25` to `0.2`. See D28 in
`DECISIONS.md` for the ruling in full, the rejected alternatives, and the
form/H2H strike-rate rule the two tokens imply.

Ranked follow-ups the reference names and this tranche does not yet
implement (a starter list for 0009b): `1x2_and_btts` (35/78/543),
`1x2_and_total_goals` (37/79/544), `total_goals_and_btts` (36),
`double_chance_and_btts` (546/540/541/542/545),
`double_chance_and_total_goals` (547/900042/900043), `multiscores` (551),
`first_and_second_half_btts` (55).

## The seven-token verdict vocabulary (D10 + D28)

    won            outcome came in                            result_value 1.00
    lost           outcome did not come in                    result_value 0.00
    push           stake-neutral / half result                result_value 0.50
    half_win       asian quarter line: one half won,          result_value 0.75
                   one half pushed (D28)
    half_loss      asian quarter line: one half lost,         result_value 0.25
                   one half pushed (D28)
    void           no verdict possible in principle (D21:     result_value NULL
                   the event did not reach FT/AET/PEN)
    unsettleable   a verdict should exist but this pass       result_value NULL
                   could not produce one (named reason)

D28 added `half_win`/`half_loss` for the asian quarter-line mixed case
(one half won and the other pushed = half-win; one half lost and the
other pushed = half-loss). Both are settled outcomes with real fractional
P&L that D10's original `push` (stake-neutral) could not faithfully
encode; collapsing a half-loss into `push` states something false about
the world and silently biases CLV/calibration on asian markets. **These
two tokens mean exactly one won half plus one pushed half, or one lost
half plus one pushed half — never "somewhere in between".** They are
produced by exactly one code path (`_settle_asian_handicap` on a
two-value quarter line whose halves disagree), and the test
`test_asian_quarter_line_half_win_and_half_loss_are_the_only_producers`
one grain up pins that invariant so a future family that accidentally
returned `half_win` would fail loudly.

`unsettleable` is the rail of the whole engine and it is the reason there is
no `0` fallback anywhere. `0` is a claim the bet **lost**; a wrong `0` is
indistinguishable downstream from a real loss, and the pipeline that reads
`fact_settlement` cannot tell whether a market with `verdict = lost` actually
lost or the calculator gave up. Every refusal here therefore lands on
`unsettleable` with a named reason — `UNSETTLEABLE_REASONS` below is
exhaustive, one counter per cause the way `betradar_outcomes.py` keeps its
four unresolved reasons apart.

The tests behind this include a mutation test
(`test_no_path_returns_lost_for_unknown`) that would fail if any refusal path
regressed to a `lost` fallback.

## Time-basis is a property of the market, never assumed (D19 + D26)

The score basis comes from `dim_market.time_basis`, computed once at market-
resolution time by `betradar_market_map.py` from the D26 rule the row records.
This engine takes the basis as an argument; it never re-derives it from the
side, the family, or the label.

    regular  -> scores.reg   (h1 + h2; NEVER includes ET even for AET fixtures)
    full     -> scores.full  (reg + et; NEVER includes penalties — see below)
    1h       -> scores.h1
    2h       -> scores.h2
    other    -> unsettleable(TIME_BASIS_OTHER) — always. D26(b) says `other`
                means "known to settle on none of D19's four bases"; the
                calculator must refuse rather than guess.

`other` is a *positive answer* about the market's basis, not an unknown; a
market whose basis has not been decided is unmapped and does not reach this
engine at all. An `other` here is the same refusal every time, by design.

## The full-vs-penalties rule, stated where it is applied

`full = reg + et`. **Penalties are excluded from `full`**, always, everywhere,
regardless of match status. The betting convention (survey `docs/fallback-
score-survey.md` section (c) reproduces the per-book evidence) is that a
shootout is a tie-break, not scored goals: sportybet's `setScore`, msport's
`OT`, and ilotbet's `reg+et` derivation all obey this. So a market whose
`time_basis = full` under a PEN status settles on the score at end of extra
time — and every 1X2 or O/U or BTTS market on a PEN match therefore settles
by regular+ET, not by shootout margin.

The rule is enforced two ways: this engine reads only `scores.full` (it never
adds penalties into full itself), and `test_full_score_never_includes_pens`
pins the invariant so that a change in either direction — a caller starting
to fold pens into `full`, or the calculator starting to add them itself —
fails visibly.

## D21's void policy, and the third status class

Only `FT`, `AET`, `PEN` settle. Every other *known* status
(`abandoned`, `not_started`, `in_progress`) returns `void` for every market —
D21 is deliberately blunt about this, and this engine is deliberately blunt
about applying it: no partial-abandonment settlement in v1, no "the market
was decided before the abandonment" special case.

An **unrecognised** status is `unsettleable(UNKNOWN_STATUS)`, never `void`.
D26(b)'s discipline applied one layer up: an unknown status is an error to
investigate — the fallback survey (0008a) already found `AET`/`AP` under
sportybet event_details are unclassified because the string does not
tokenise cleanly — and silently voiding it would drop a class of markets
without evidence that they should be voided.

## Scope: the first tranche

D20's v1 scope is large (bare 1X2 through team totals through combinations),
so this task implements the first tranche bounded by `TRANCHE_FAMILIES` —
the score/result families that D20 puts in scope and that resolve on
D18's period-resolved scores without needing per-id structured decoding
(exact_goals, goal_range, winning_margin and the goalscorer families use
`sr:*` structured outcome ids whose decoded meaning is not carried by the
map and would need its own per-id lookup — deferred to a later tranche).

Everything **outside** the tranche returns `unsettleable(FAMILY_NOT_IMPLEMENTED)`,
counted rather than guessed. Player-prop families (D13) return their own
`PLAYER_PROP_NOT_IMPLEMENTED` reason so a later "prop crosswalk" pass and a
"more score markets" pass can be ranked independently.

## `calculator_version` and what constitutes a bump

`CALCULATOR_VERSION` is a named constant stamped by the settlement loader
(0010) into `fact_settlement.calculator_version`. **Bump it whenever a
verdict this calculator produces can change** for the same input tuple
`(scores, status, family, period, time_basis, side_or_line)`:

- a new family joins the tranche (a previously `unsettleable` input starts
  returning `won`/`lost`/`push`);
- an existing family's logic changes (a corrected O/U push rule, a corrected
  handicap tie-break, a corrected correct-score `other` bucket);
- the time-basis dispatch changes for any basis;
- the full-vs-penalties rule changes.

Do NOT bump for: docstring edits, refactors that leave every verdict
unchanged, a new reason token added under `UNSETTLEABLE_REASONS` for a case
that was already `unsettleable` under a coarser reason (the token grain of
`reason` is not part of the verdict). D4's mechanism then re-settles the
affected partitions on the next pass, because a watermark alone would leave
old-logic rows in place under the old stamp.

## side_or_line, parsed in 0005d's documented format

The one format `betradar_outcome_map.py` established:

    side_or_line := <side>                       (family declares no line key)
                  | <side> "@" <line>            (one or more line values)

    <line>       := values joined by ";"

`<side>` is either a canonical token from the family's `sides` vocabulary
(`home`/`over`/`yes`/…) or a betradar structured id verbatim
(`sr:player:*`/`sr:exact_goals:*`/`sr:goal_range:*`). `<line>` values are
already canonicalised by `betradar_outcome_map.canonical_line_value` before
they reach the loader; this engine treats numeric lines as `Decimal` — never
`float`, per `fact_odds`'s own DDL rail. Two books writing "2.50" and "2.5"
resolve to the same `side_or_line` before they reach here, so a comparison
that fails identity would fail everywhere in the platform, not just here.

## Identity is ids and canonical sides, never labels (D12)

The engine reads `market_family` and canonical side/line tokens — never a
book's own label. `betradar_outcome_map.py`'s docstring proves the point:
outcome `10` is "1/2" to ilotbet, "1 2" to msport and "Home or Away" to
sportybet. Reading a label would make a book's phrasing part of a settled
verdict, which is exactly the thing D12 abolished for identity and which
D10 abolishes for verdicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

# --- versioning --------------------------------------------------------------

#: Stamped into `fact_settlement.calculator_version` by 0010. Bump per the
#: rule in the module docstring; the stamp is what tells D4 which rows the
#: next re-settle pass owes.
#:
#: 0009b bump: `_settle_asian_handicap` now returns `half_win`/`half_loss`
#: on the mixed quarter-line case instead of collapsing to `push`. That
#: flips a verdict for the same input tuple `(scores, status, family,
#: period, time_basis, side_or_line)` — exactly the docstring's own bump
#: rule — so 0010's re-settle pass will re-write those rows under the new
#: stamp when the loader lands.
#:
#: 0009c bump: the tranche gains the combination and range/bucket families
#: listed in `TRANCHE_ADDITIONS_009C`, plus `correct_score` at `1h`/`2h`.
#: Every one of those inputs previously returned `unsettleable(family_
#: not_implemented)` and now returns a settled verdict — a verdict flip
#: for the same input tuple, so the stamp moves and 0010's re-settle
#: pass will re-write the affected rows under the new stamp when the
#: loader lands.
CALCULATOR_VERSION: Final = "0009c.1"

# --- the seven verdict tokens (D10 + D28) ------------------------------------
#
# D28 added `half_win` (0.75) and `half_loss` (0.25) for the asian
# quarter-line mixed case. See the module docstring for the vocabulary
# table and the note on why these two tokens must not become a dumping
# ground for anything else.

VERDICT_WON: Final = "won"
VERDICT_LOST: Final = "lost"
VERDICT_PUSH: Final = "push"
VERDICT_HALF_WON: Final = "half_win"
VERDICT_HALF_LOST: Final = "half_loss"
VERDICT_VOID: Final = "void"
VERDICT_UNSETTLEABLE: Final = "unsettleable"

VERDICTS: Final = (
    VERDICT_WON,
    VERDICT_LOST,
    VERDICT_PUSH,
    VERDICT_HALF_WON,
    VERDICT_HALF_LOST,
    VERDICT_VOID,
    VERDICT_UNSETTLEABLE,
)

# --- statuses (D21), matching `survey.score_shapes` tokens -------------------

STATUS_FT: Final = "ft"
STATUS_AET: Final = "aet"
STATUS_PEN: Final = "pen"
STATUS_NOT_STARTED: Final = "not_started"
STATUS_IN_PROGRESS: Final = "in_progress"
STATUS_ABANDONED: Final = "abandoned"

#: D21's three settlable classes. Every other *known* status voids; an
#: unknown status is `unsettleable(UNKNOWN_STATUS)`, never silently void.
SETTLABLE_STATUSES: Final = frozenset({STATUS_FT, STATUS_AET, STATUS_PEN})
KNOWN_STATUSES: Final = frozenset(
    {*SETTLABLE_STATUSES, STATUS_NOT_STARTED, STATUS_IN_PROGRESS, STATUS_ABANDONED}
)

# --- time-basis tokens, identical to `dim_market.time_basis` (D19 + D26) -----

BASIS_REGULAR: Final = "regular"
BASIS_FULL: Final = "full"
BASIS_1H: Final = "1h"
BASIS_2H: Final = "2h"
BASIS_OTHER: Final = "other"

BASES: Final = (BASIS_REGULAR, BASIS_FULL, BASIS_1H, BASIS_2H, BASIS_OTHER)

# --- unsettleable reasons ----------------------------------------------------
#
# One per cause, kept apart the way `betradar_outcomes.py` keeps its four
# unresolved reasons apart. A single "unresolved" counter would hide which
# case fired and turn a hole in the side vocabulary into background noise
# behind a family-not-implemented count that dwarfs it.

REASON_UNKNOWN_STATUS: Final = "unknown_status"
REASON_FAMILY_NOT_IMPLEMENTED: Final = "family_not_implemented"
REASON_PLAYER_PROP_NOT_IMPLEMENTED: Final = "player_prop_not_implemented"
REASON_TIME_BASIS_OTHER: Final = "time_basis_other"
REASON_TIME_BASIS_UNKNOWN: Final = "time_basis_unknown"
REASON_MISSING_SCORE_COMPONENT: Final = "missing_score_component"
REASON_UNPARSEABLE_SIDE_OR_LINE: Final = "unparseable_side_or_line"
REASON_UNKNOWN_SIDE: Final = "unknown_side"
REASON_LINE_INVALID: Final = "line_invalid"

UNSETTLEABLE_REASONS: Final = (
    REASON_UNKNOWN_STATUS,
    REASON_FAMILY_NOT_IMPLEMENTED,
    REASON_PLAYER_PROP_NOT_IMPLEMENTED,
    REASON_TIME_BASIS_OTHER,
    REASON_TIME_BASIS_UNKNOWN,
    REASON_MISSING_SCORE_COMPONENT,
    REASON_UNPARSEABLE_SIDE_OR_LINE,
    REASON_UNKNOWN_SIDE,
    REASON_LINE_INVALID,
)


# --- input types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TeamScore:
    """One (home, away) pair. Integers, because goals are counts."""

    home: int
    away: int

    @property
    def total(self) -> int:
        return self.home + self.away


@dataclass(frozen=True, slots=True)
class PeriodResolvedScores:
    """D18's period-resolved score set. Any component may be None if the
    upstream source did not publish it — this engine refuses with
    `MISSING_SCORE_COMPONENT` rather than substitute.

    Invariant on `full` (stated here, tested in `tests/test_settlement.py`):
    `full = reg + et`, and **penalties are never folded in**. Every fallback
    source in `docs/fallback-score-survey.md` (c) obeys this convention; a
    caller populating `full` from a shootout total is a bug in the caller,
    not something this engine tries to detect.
    """

    h1: TeamScore | None = None
    h2: TeamScore | None = None
    reg: TeamScore | None = None
    et: TeamScore | None = None
    pens: TeamScore | None = None
    full: TeamScore | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    """One `fact_settlement` verdict, with the reason on unsettleable rows.

    `reason` is populated **only** when `verdict == unsettleable`. A settled
    verdict (won/lost/push/void) never carries a reason — the reason is the
    verdict.
    """

    verdict: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {self.verdict!r}")
        if self.verdict == VERDICT_UNSETTLEABLE and self.reason is None:
            raise ValueError("unsettleable verdicts must carry a reason")
        if self.verdict != VERDICT_UNSETTLEABLE and self.reason is not None:
            raise ValueError(
                f"non-unsettleable verdict {self.verdict!r} must not carry a reason"
            )


# --- side/line parsing (0005d's format) --------------------------------------

_LINE_SEPARATOR: Final = "@"
_LINE_VALUE_SEPARATOR: Final = ";"


@dataclass(frozen=True, slots=True)
class SideAndLine:
    """A parsed `side_or_line`: the canonical side and its (possibly empty)
    ordered line values, verbatim strings.
    """

    side: str
    line_values: tuple[str, ...]


def parse_side_or_line(text: str) -> SideAndLine | None:
    """Split `side[@line[;line…]]` — return None on an unparseable input.

    Empty side, empty text and a stray leading `@` are all unparseable; a
    trailing `@` with no line values likewise. Returning None keeps the
    caller in charge of the refusal — this parser produces no verdict of its
    own, exactly the shape `betradar_outcome_map.canonical_line_value` follows
    one grain up.
    """
    if not isinstance(text, str) or not text:
        return None
    if _LINE_SEPARATOR not in text:
        return SideAndLine(side=text, line_values=())
    side, _, line = text.partition(_LINE_SEPARATOR)
    if not side or not line:
        return None
    values = tuple(line.split(_LINE_VALUE_SEPARATOR))
    if any(v == "" for v in values):
        return None
    return SideAndLine(side=side, line_values=values)


def _decimal_line(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


# --- tranche of families implemented this task -------------------------------
#
# The first tranche. Chosen from D20's in-scope score/result families with a
# rule: include a family iff its side vocabulary is either
#
#   - a fixed canonical token set (`home`/`draw`/`away`, `over`/`under`,
#     `yes`/`no`, `home_or_draw`/…, `odd`/`even`, `1h`/`2h`/`equal`,
#     `only_home`/…), or
#   - a `H:A` pair whose semantics are period-scoped and locally enumerated
#     (`correct_score` at match), or
#   - a `<h_result>_<f_result>` pair (`halftime_fulltime`), or
#   - a range bucket like `1-2` / `7+` / `no_goal` (`multigoals`).
#
# Excluded from *this* tranche, and returning `family_not_implemented` here:
#
#   - Families whose sides are `sr:exact_goals:*`/`sr:goal_range:*`/
#     `sr:winning_margin:*` structured ids: each id encodes a variant AND a
#     bucket, and the map does not carry the bucket decoding. A tranche that
#     included these would ship a lookup table without evidence for its
#     values, exactly the thing 0005b/0005e refused to invent.
#   - Every combination market (`1x2_and_*`, `double_chance_and_*`,
#     `halftime_fulltime_and_*`, `first_and_second_half_btts`,
#     `xth_goal_and_1x2`, `multiscores`, `halftime_fulltime_correct_score`,
#     `goal_bounds*`, `excluded_goals*`): each pairs two decisions and
#     deserves its own reviewable handler rather than a bundle.
#   - `next_goal` / `last_goal` / `teams_to_score` when the score alone does
#     not decide — "who scored the Xth goal" cannot be answered from a total,
#     it needs `fact_match_event`, which 0008 owns and 0009a does not read.
#     `teams_to_score` (`none`/`only_home`/`only_away`/`both`) DOES decide
#     from `(home, away)` alone and is included.
#   - `goal_streak_*`, `no_draw_and_btts`, `home_to_win`/`away_to_win`/
#     `any_team_to_win`: small vocabularies, low priority, deferred as a
#     bundle to a later tranche.
#   - Every family whose `dim_market.time_basis` is `other` — refused by the
#     time-basis rail long before it reaches a family handler.
#   - Every player-prop family (D13, v2): refused with a distinct reason so
#     the two backlogs (prop crosswalk vs more score markets) do not merge.
_TRANCHE_FAMILIES_009A: Final = frozenset(
    {
        "1x2",
        "double_chance",
        "draw_no_bet",
        "home_no_bet",
        "away_no_bet",
        "total_goals",
        "total_goals_home",
        "total_goals_away",
        "btts",
        "correct_score",
        "halftime_fulltime",
        "odd_even_goals",
        "odd_even_home",
        "odd_even_away",
        "handicap",
        "asian_handicap",
        "teams_to_score",
        "clean_sheet_home",
        "clean_sheet_away",
        "win_to_nil_home",
        "win_to_nil_away",
        "highest_scoring_half",
        "highest_scoring_half_home",
        "highest_scoring_half_away",
        "both_halves_over",
        "both_halves_under",
        "score_in_both_halves_home",
        "score_in_both_halves_away",
        "win_both_halves_home",
        "win_both_halves_away",
        "win_either_half_home",
        "win_either_half_away",
        "multigoals",
        "multigoals_home",
        "multigoals_away",
    }
)

#: 0009c's added families. Chosen top-down from the 0009b priced-volume
#: ranking (`docs/market-map-coverage.md`, largest-unsettleable-families
#: table), skipping only families a `tranche_hole` label cannot cover with
#: what silver knows today — each such skip is named in the docstring
#: comment beside this set.
#:
#: The set is a **named constant** because the tranche's stopping rule is
#: itself a claim about the work done, exactly the way `TOP_N_MAPPED`
#: fixed 0007b's stopping rule as a claim rather than as a
#: cycle-of-editing side effect.
#:
#: Families deliberately NOT in this tranche (with reason):
#:
#: - `exact_goals`, `exact_goals_home`, `exact_goals_away`, `goal_range`,
#:   `winning_margin` — their canonical `side_or_line` is a betradar
#:   structured id (`sr:exact_goals:3+:88`, `sr:goal_range:7+:1342`,
#:   `sr:winning_margin:3+:113`), with the outcome bucket encoded inside
#:   the id's last component. That bucket cannot be decoded here without
#:   either (a) an evidence-backed per-id lookup table, which no live
#:   source in this repo carries, or (b) reading a book's label (D12
#:   forbids), or (c) inferring the bucket from the id's numeric part —
#:   which the task fence forbids and 0005e's `810001`/`810002` case is
#:   the standing proof of ("the same id means different buckets in
#:   different families"). Refused as `family_not_implemented` until an
#:   evidence table lands.
#: - `next_goal`, `last_goal`, `xth_goal_and_1x2` — settling any of these
#:   requires knowing which team scored the Nth goal, which is
#:   `fact_match_event`'s grain (task 0008), not derivable from a
#:   `(home, away)` score alone. Not a tranche gap; a data-dependency
#:   gap 0009c cannot close.
#: - `excluded_goals`, `excluded_goals_home`, `excluded_goals_away`,
#:   `excluded_goals_first_half` — the "excluded" semantic (does the
#:   side mean "the total was N" or "N was NOT the total"?) is not
#:   evident from the map or the label alone, and no live payload
#:   surfaces the settlement rule. Refused pending evidence rather
#:   than guessed — same rule as the structured-id families above.
TRANCHE_ADDITIONS_009C: Final = frozenset(
    {
        # combination families whose legs read one basis-selected score
        # (basis chosen by the map's own `time_basis` per D26; 1X2 leg
        # reads the basis score, BTTS/OU leg reads it too).
        "1x2_and_btts",
        "1x2_and_total_goals",
        "double_chance_and_btts",
        "double_chance_and_total_goals",
        "total_goals_and_btts",
        # combination families whose legs read DIFFERENT D18 components
        # under one basis (D26(c)): HT/FT reads h1 + reg, other leg
        # reads whichever component the label names. `regular` gives the
        # calculator every component the market needs; the per-leg
        # component map lives in the handler's docstring.
        "halftime_fulltime_and_total_goals",
        "halftime_fulltime_and_first_half_total_goals",
        "halftime_fulltime_and_exact_goals",
        "halftime_fulltime_correct_score",
        "double_chance_and_first_half_btts",
        "double_chance_and_second_half_btts",
        "first_and_second_half_btts",
        # bucket-vocabulary families. `multiscores` sides are score-set
        # tokens (e.g. "1:0_2:0_or_3:0"), decoded here from the canonical
        # tokens the outcome map produced (D12). `goal_bounds` sides are
        # bucket ranges (`0-2`, `1-3+`, `5+`); the parser here handles
        # the `N-M+` range that `multigoals` does not.
        "multiscores",
        "goal_bounds",
        "goal_bounds_home",
        "goal_bounds_away",
        "goal_bounds_first_half",
    }
)

TRANCHE_FAMILIES: Final = _TRANCHE_FAMILIES_009A | TRANCHE_ADDITIONS_009C

#: Player-prop families that must return a distinct reason from
#: `family_not_implemented`. Sourced from `betradar_market_map.is_player_prop`,
#: hard-coded here so this module remains pure (no import-time introspection
#: of the map — a data change is a deliberate edit).
_PLAYER_PROP_FAMILIES: Final = frozenset(
    {
        "goalscorer_nth",
        "goalscorer_last",
        "goalscorer_anytime",
        "player_to_score_two_plus",
        "player_to_score_three_plus",
        "player_fouls_won",
        "player_not_to_score",
    }
)


# --- helpers -----------------------------------------------------------------


def _unsettleable(reason: str) -> Verdict:
    return Verdict(verdict=VERDICT_UNSETTLEABLE, reason=reason)


def _won_or_lost(condition: bool) -> Verdict:
    return Verdict(verdict=VERDICT_WON) if condition else Verdict(verdict=VERDICT_LOST)


def _select_score(scores: PeriodResolvedScores, basis: str) -> TeamScore | None:
    """Pick the score component the basis refers to. None if not published.

    Never adds ET or PENS into `full` — the caller is responsible for D18's
    invariant (`full = reg + et`, pens excluded), and reading a component the
    caller did not publish is precisely `missing_score_component`.
    """
    if basis == BASIS_REGULAR:
        return scores.reg
    if basis == BASIS_FULL:
        return scores.full
    if basis == BASIS_1H:
        return scores.h1
    if basis == BASIS_2H:
        return scores.h2
    return None  # BASIS_OTHER / unknown handled by settle()


# --- family handlers ---------------------------------------------------------
#
# Each handler receives the resolved TeamScore for the basis, plus the parsed
# side and line values. None of them re-selects the basis or re-checks the
# status; those decisions are made once, in settle(), before dispatch. A
# handler returns a Verdict or None if the family is out-of-tranche — but
# every family in TRANCHE_FAMILIES has a handler, so None here means an edit
# out-of-sync with TRANCHE_FAMILIES and the top-level test catches it.


def _cmp_1x2(home: int, away: int) -> str:
    """`home`/`draw`/`away` from a (home, away) score. Same helper as the
    reference script in `arbibet-dash/scripts/settlement_test.py:76` used,
    kept here so the AET divergence test reproduces its exact logic."""
    if home > away:
        return "home"
    if away > home:
        return "away"
    return "draw"


def _settle_1x2(score: TeamScore, s: SideAndLine) -> Verdict:
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    if s.side not in {"home", "draw", "away"}:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return _won_or_lost(s.side == _cmp_1x2(score.home, score.away))


def _settle_double_chance(score: TeamScore, s: SideAndLine) -> Verdict:
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    outcome = _cmp_1x2(score.home, score.away)
    covers = {
        "home_or_draw": {"home", "draw"},
        "home_or_away": {"home", "away"},
        "draw_or_away": {"draw", "away"},
    }
    if s.side not in covers:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return _won_or_lost(outcome in covers[s.side])


def _settle_dnb_variants(family: str, score: TeamScore, s: SideAndLine) -> Verdict:
    """`draw_no_bet` / `home_no_bet` / `away_no_bet`. The "no bet" leg pushes."""
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    outcome = _cmp_1x2(score.home, score.away)
    if family == "draw_no_bet":
        if outcome == "draw":
            return Verdict(verdict=VERDICT_PUSH)
        if s.side in {"home", "away"}:
            return _won_or_lost(s.side == outcome)
    elif family == "home_no_bet":
        if outcome == "home":
            return Verdict(verdict=VERDICT_PUSH)
        if s.side in {"draw", "away"}:
            return _won_or_lost(s.side == outcome)
    else:  # away_no_bet
        if outcome == "away":
            return Verdict(verdict=VERDICT_PUSH)
        if s.side in {"home", "draw"}:
            return _won_or_lost(s.side == outcome)
    return _unsettleable(REASON_UNKNOWN_SIDE)


def _team_total(score: TeamScore, family: str) -> int:
    """The goals a `total_goals*` variant reads. `total_goals_home` reads only
    the home goals; the "the *outcomes* of a team total are Over/Under, so
    'home' is part of what the market is" note in
    `betradar_market_map.py:290-300` is what earns each of these its own
    family rather than a side."""
    if family == "total_goals_home":
        return score.home
    if family == "total_goals_away":
        return score.away
    return score.total


def _settle_total_goals(family: str, score: TeamScore, s: SideAndLine) -> Verdict:
    if len(s.line_values) != 1:
        return _unsettleable(REASON_LINE_INVALID)
    line = _decimal_line(s.line_values[0])
    if line is None:
        return _unsettleable(REASON_LINE_INVALID)
    if s.side not in {"over", "under"}:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    total = Decimal(_team_total(score, family))
    # Exact decimal comparison (`fact_odds`'s own rail): `2.5` vs `3.0` never
    # ties, `2.0` vs `2.0` pushes.
    if total == line:
        return Verdict(verdict=VERDICT_PUSH)
    over = total > line
    return _won_or_lost((s.side == "over" and over) or (s.side == "under" and not over))


def _settle_btts(score: TeamScore, s: SideAndLine) -> Verdict:
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    if s.side not in {"yes", "no"}:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    both = score.home > 0 and score.away > 0
    return _won_or_lost((s.side == "yes") == both)


def _settle_odd_even(family: str, score: TeamScore, s: SideAndLine) -> Verdict:
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    if s.side not in {"odd", "even"}:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    if family == "odd_even_home":
        goals = score.home
    elif family == "odd_even_away":
        goals = score.away
    else:
        goals = score.total
    is_odd = (goals % 2) == 1
    return _won_or_lost((s.side == "odd") == is_odd)


def _settle_handicap(score: TeamScore, s: SideAndLine) -> Verdict:
    """1X2 handicap: line is a goal pair `H:A`, e.g. `0:1` means the home
    team is spotted 0, away spotted 1 — settle 1X2 on `(home+H, away+A)`."""
    if len(s.line_values) != 1:
        return _unsettleable(REASON_LINE_INVALID)
    pair = s.line_values[0]
    if ":" not in pair:
        return _unsettleable(REASON_LINE_INVALID)
    left, _, right = pair.partition(":")
    try:
        h_adj = int(left)
        a_adj = int(right)
    except ValueError:
        return _unsettleable(REASON_LINE_INVALID)
    if s.side not in {"home", "draw", "away"}:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    outcome = _cmp_1x2(score.home + h_adj, score.away + a_adj)
    return _won_or_lost(s.side == outcome)


def _settle_asian_handicap(score: TeamScore, s: SideAndLine) -> Verdict:
    """Asian handicap: line is one or two Decimals. A single value settles
    with a push on the exact tie; two values (a quarter line) settle each
    half of the stake against its own line and combine as follows (D28):

        wholly won   both halves won            -> won
        wholly lost  both halves lost           -> lost
        wholly push  both halves pushed         -> push
        half-win     one won + one pushed       -> half_win
        half-loss    one lost + one pushed      -> half_loss

    The two mixed cases D28 added are the only paths in this engine that
    return `half_win` / `half_loss`. Any other combination (won + lost,
    which is arithmetically impossible on a real quarter line — the two
    halves are half a goal apart, so a margin cannot land strictly above
    one and strictly below the other) is unsettleable(LINE_INVALID) so a
    caller supplying a nonsense line pair does not silently produce a
    fake half-outcome.
    """
    if not s.line_values or len(s.line_values) > 2:
        return _unsettleable(REASON_LINE_INVALID)
    if s.side not in {"home", "away"}:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    lines: list[Decimal] = []
    for value in s.line_values:
        line = _decimal_line(value)
        if line is None:
            return _unsettleable(REASON_LINE_INVALID)
        lines.append(line)
    sign = Decimal(1) if s.side == "home" else Decimal(-1)
    margin = Decimal(score.home - score.away) * sign
    results: list[str] = []
    for line in lines:
        adjusted = margin + line
        if adjusted > 0:
            results.append(VERDICT_WON)
        elif adjusted < 0:
            results.append(VERDICT_LOST)
        else:
            results.append(VERDICT_PUSH)
    if len(results) == 1:
        return Verdict(verdict=results[0])
    if results[0] == results[1]:
        return Verdict(verdict=results[0])
    # Mixed quarter-line outcomes (D28). One half won and one half pushed
    # is a `half_win`; one half lost and one half pushed is a `half_loss`.
    # Both members of the pair are always considered so a caller cannot
    # smuggle in an impossible line pair (won + lost) and silently produce
    # a half-outcome — anything not matching a documented pair is
    # `line_invalid`, the same refusal a nonsensical line already gets.
    pair = frozenset(results)
    if pair == frozenset({VERDICT_WON, VERDICT_PUSH}):
        return Verdict(verdict=VERDICT_HALF_WON)
    if pair == frozenset({VERDICT_LOST, VERDICT_PUSH}):
        return Verdict(verdict=VERDICT_HALF_LOST)
    return _unsettleable(REASON_LINE_INVALID)


# The correct-score enumerations. Each period's enumeration is the set of
# `H:A` sides betradar publishes for it — the id blocks in
# `betradar_outcome_map.SIDES_CORRECT_SCORE` (`45` match, `81` 1st half,
# `98` 2nd half). "other" wins iff the actual score is not in the
# enumeration for that period; if the enumerations for the halves matched
# the match's, "other" would settle differently despite the same id block,
# which is the whole reason the map keeps three disjoint blocks (D16).
_CORRECT_SCORE_MATCH_ENUM: Final = frozenset(
    f"{h}:{a}" for h in range(5) for a in range(5)
)
_CORRECT_SCORE_HALF_ENUM: Final = frozenset(
    f"{h}:{a}" for h in range(3) for a in range(3)
)


def _settle_correct_score(
    period: str, score: TeamScore, s: SideAndLine
) -> Verdict:
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    if period == "match":
        enum = _CORRECT_SCORE_MATCH_ENUM
    elif period in {"1h", "2h"}:
        # Same 3x3 grid for both halves — betradar publishes the same
        # nine scores plus `other` under `81` and `98`, per the outcome
        # map's `SIDES_CORRECT_SCORE`. Score component selection is the
        # top-level basis's job (`1h` picks `h1`, `2h` picks `h2`); this
        # handler only enumerates.
        enum = _CORRECT_SCORE_HALF_ENUM
    else:
        return _unsettleable(REASON_FAMILY_NOT_IMPLEMENTED)
    actual = f"{score.home}:{score.away}"
    if s.side == "other":
        return _won_or_lost(actual not in enum)
    if ":" not in s.side:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    if s.side not in enum:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return _won_or_lost(s.side == actual)


def _settle_halftime_fulltime(
    scores: PeriodResolvedScores, s: SideAndLine
) -> Verdict:
    """Reads both `h1` and `reg` (D18's h1 alongside the regular-time
    result — see `halftime_fulltime`'s note in `betradar_market_map.py`).
    Time-basis is `regular`, but the *ht* leg needs `h1` too, which is why
    D18 stores period-resolved rather than one total (the whole reason for
    it)."""
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    if scores.h1 is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    ht = _cmp_1x2(scores.h1.home, scores.h1.away)
    # `_settle_halftime_fulltime` is called only from the regular-basis path,
    # so `scores.reg` is guaranteed non-None (the top-level check ran).
    assert scores.reg is not None
    ft = _cmp_1x2(scores.reg.home, scores.reg.away)
    if "_" not in s.side:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    ht_leg, _, ft_leg = s.side.partition("_")
    allowed = {"home", "draw", "away"}
    if ht_leg not in allowed or ft_leg not in allowed:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return _won_or_lost(ht_leg == ht and ft_leg == ft)


def _settle_yes_no(condition: bool, s: SideAndLine) -> Verdict:
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    if s.side not in {"yes", "no"}:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return _won_or_lost((s.side == "yes") == condition)


def _settle_clean_sheet(family: str, score: TeamScore, s: SideAndLine) -> Verdict:
    # Home clean sheet: away scored zero. Away clean sheet: home scored zero.
    condition = score.away == 0 if family == "clean_sheet_home" else score.home == 0
    return _settle_yes_no(condition, s)


def _settle_win_to_nil(family: str, score: TeamScore, s: SideAndLine) -> Verdict:
    if family == "win_to_nil_home":
        condition = score.home > score.away and score.away == 0
    else:
        condition = score.away > score.home and score.home == 0
    return _settle_yes_no(condition, s)


def _settle_highest_scoring_half(
    scope: str, scores: PeriodResolvedScores, s: SideAndLine
) -> Verdict:
    """`scope` is `match`, `home` or `away`. Compares h1 and h2 goals for
    the scope's team(s) — needs both halves; missing either is
    `missing_score_component`."""
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    if scores.h1 is None or scores.h2 is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    if s.side not in {"1h", "2h", "equal"}:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    if scope == "match":
        first = scores.h1.total
        second = scores.h2.total
    elif scope == "home":
        first = scores.h1.home
        second = scores.h2.home
    else:  # away
        first = scores.h1.away
        second = scores.h2.away
    if first > second:
        winner = "1h"
    elif second > first:
        winner = "2h"
    else:
        winner = "equal"
    return _won_or_lost(s.side == winner)


def _settle_both_halves_over(
    scores: PeriodResolvedScores, side_yes: bool, s: SideAndLine
) -> Verdict:
    """`both_halves_over` / `both_halves_under` with a `total` line — reads
    both halves' totals; either half missing is `missing_score_component`.
    """
    if len(s.line_values) != 1:
        return _unsettleable(REASON_LINE_INVALID)
    line = _decimal_line(s.line_values[0])
    if line is None:
        return _unsettleable(REASON_LINE_INVALID)
    if s.side not in {"yes", "no"}:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    if scores.h1 is None or scores.h2 is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    h1_total = Decimal(scores.h1.total)
    h2_total = Decimal(scores.h2.total)
    # A push on either half collapses the whole bet to unsettleable at this
    # grain rather than guessing: match-total O/U pushes at half-goal lines
    # cannot happen (`.5` avoids the tie), but `1.0` and `2.0` on a half do,
    # and reading either half as a push under a `both halves over` bet is a
    # rule with more than one plausible reading. Refused explicitly.
    if h1_total == line or h2_total == line:
        return _unsettleable(REASON_LINE_INVALID)
    if side_yes:
        # `both_halves_over yes` / `both_halves_under no`
        both = (h1_total > line) and (h2_total > line)
    else:
        both = (h1_total < line) and (h2_total < line)
    return _won_or_lost(s.side == "yes" and both or s.side == "no" and not both)


def _settle_score_in_both_halves(
    team: str, scores: PeriodResolvedScores, s: SideAndLine
) -> Verdict:
    if scores.h1 is None or scores.h2 is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    if team == "home":
        condition = scores.h1.home > 0 and scores.h2.home > 0
    else:
        condition = scores.h1.away > 0 and scores.h2.away > 0
    return _settle_yes_no(condition, s)


def _settle_win_both_halves(
    team: str, scores: PeriodResolvedScores, s: SideAndLine
) -> Verdict:
    if scores.h1 is None or scores.h2 is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    if team == "home":
        c = scores.h1.home > scores.h1.away and scores.h2.home > scores.h2.away
    else:
        c = scores.h1.away > scores.h1.home and scores.h2.away > scores.h2.home
    return _settle_yes_no(c, s)


def _settle_win_either_half(
    team: str, scores: PeriodResolvedScores, s: SideAndLine
) -> Verdict:
    if scores.h1 is None or scores.h2 is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    if team == "home":
        c = (scores.h1.home > scores.h1.away) or (scores.h2.home > scores.h2.away)
    else:
        c = (scores.h1.away > scores.h1.home) or (scores.h2.away > scores.h2.home)
    return _settle_yes_no(c, s)


def _settle_teams_to_score(score: TeamScore, s: SideAndLine) -> Verdict:
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    home_scored = score.home > 0
    away_scored = score.away > 0
    if not home_scored and not away_scored:
        actual = "none"
    elif home_scored and not away_scored:
        actual = "only_home"
    elif away_scored and not home_scored:
        actual = "only_away"
    else:
        actual = "both"
    if s.side not in {"none", "only_home", "only_away", "both"}:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return _won_or_lost(s.side == actual)


def _multigoals_bucket(goals: int, side: str) -> Verdict:
    """`side` is one of `1-2`/`1-3`/…/`4+`/`5+`/`7+`/`no_goal`. Returns
    won/lost from the goals count. `no_goal` is a real side value
    (`SIDES_MULTIGOALS`'s `1804`/`1805`), not a synonym for zero."""
    if side == "no_goal":
        return _won_or_lost(goals == 0)
    if side.endswith("+"):
        try:
            threshold = int(side[:-1])
        except ValueError:
            return _unsettleable(REASON_UNKNOWN_SIDE)
        return _won_or_lost(goals >= threshold)
    if "-" not in side:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    left, _, right = side.partition("-")
    try:
        low = int(left)
        high = int(right)
    except ValueError:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return _won_or_lost(low <= goals <= high)


def _settle_multigoals(family: str, score: TeamScore, s: SideAndLine) -> Verdict:
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    if family == "multigoals_home":
        goals = score.home
    elif family == "multigoals_away":
        goals = score.away
    else:
        goals = score.total
    return _multigoals_bucket(goals, s.side)


# --- 0009c: combination and bucket-vocabulary families ----------------------


_1X2_TOKENS: Final = frozenset({"home", "draw", "away"})
_DC_TOKENS: Final = frozenset({"home_or_draw", "home_or_away", "draw_or_away"})
_DC_COVERS: Final[dict[str, frozenset[str]]] = {
    "home_or_draw": frozenset({"home", "draw"}),
    "home_or_away": frozenset({"home", "away"}),
    "draw_or_away": frozenset({"draw", "away"}),
}
_OU_TOKENS: Final = frozenset({"over", "under"})
_YES_NO_TOKENS: Final = frozenset({"yes", "no"})
_COMBO_SEPARATOR: Final = "_and_"


def _split_combo(side: str) -> tuple[str, str] | None:
    """Split a combination side on `_and_`, using rpartition so a first
    leg carrying its own underscore (e.g. `home_or_draw`) stays intact."""
    if _COMBO_SEPARATOR not in side:
        return None
    left, sep, right = side.rpartition(_COMBO_SEPARATOR)
    if not left or not right or sep != _COMBO_SEPARATOR:
        return None
    return (left, right)


def _combine_legs(leg1: str, leg2: str) -> str:
    """Combine two leg verdicts under standard book combo rules:
    all-won → won; any lost → lost; else (has push) → push. Never emits
    `half_win`/`half_loss` — D28's two new tokens are produced by exactly
    one path (`_settle_asian_handicap` on a mixed quarter line), and this
    helper carries no path to them (the sweep-across-non-asian-families
    test pins the invariant one grain up)."""
    if leg1 == VERDICT_LOST or leg2 == VERDICT_LOST:
        return VERDICT_LOST
    if leg1 == VERDICT_WON and leg2 == VERDICT_WON:
        return VERDICT_WON
    return VERDICT_PUSH


def _cmp_ou(total: int, line: Decimal, side: str) -> str:
    """`won`/`lost`/`push` for an over/under leg, with exact Decimal
    equality (`fact_odds`'s own rail): a whole-number line ties push."""
    total_dec = Decimal(total)
    if total_dec == line:
        return VERDICT_PUSH
    over = total_dec > line
    if side == "over":
        return VERDICT_WON if over else VERDICT_LOST
    return VERDICT_WON if not over else VERDICT_LOST


def _cmp_btts(score: TeamScore, side: str) -> str:
    """`won`/`lost` for a BTTS leg — no push case."""
    both = score.home > 0 and score.away > 0
    if side == "yes":
        return VERDICT_WON if both else VERDICT_LOST
    return VERDICT_WON if not both else VERDICT_LOST


def _cmp_1x2_leg(score: TeamScore, side: str) -> str:
    """`won`/`lost` for a 1X2 leg — no push case (1X2 always decides)."""
    return VERDICT_WON if side == _cmp_1x2(score.home, score.away) else VERDICT_LOST


def _cmp_dc_leg(score: TeamScore, side: str) -> str:
    """`won`/`lost` for a Double Chance leg — no push case."""
    outcome = _cmp_1x2(score.home, score.away)
    return VERDICT_WON if outcome in _DC_COVERS[side] else VERDICT_LOST


def _settle_1x2_and_btts(score: TeamScore, s: SideAndLine) -> Verdict:
    """Both legs read the basis-selected score — one D18 component per
    call (`regular` for `35`, `1h` for `78`, `2h` for `543`)."""
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    parts = _split_combo(s.side)
    if parts is None:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    r_side, btts_side = parts
    if r_side not in _1X2_TOKENS or btts_side not in _YES_NO_TOKENS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return Verdict(
        verdict=_combine_legs(_cmp_1x2_leg(score, r_side), _cmp_btts(score, btts_side))
    )


def _settle_double_chance_and_btts(score: TeamScore, s: SideAndLine) -> Verdict:
    """Both legs read the basis-selected score. Match-level `546`,
    1h `542`, 2h `545`."""
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    parts = _split_combo(s.side)
    if parts is None:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    dc_side, btts_side = parts
    if dc_side not in _DC_TOKENS or btts_side not in _YES_NO_TOKENS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return Verdict(
        verdict=_combine_legs(_cmp_dc_leg(score, dc_side), _cmp_btts(score, btts_side))
    )


def _settle_1x2_and_total_goals(score: TeamScore, s: SideAndLine) -> Verdict:
    """Both legs read the basis-selected score. Match-level `37`,
    1h `79`, 2h `544`."""
    if len(s.line_values) != 1:
        return _unsettleable(REASON_LINE_INVALID)
    line = _decimal_line(s.line_values[0])
    if line is None:
        return _unsettleable(REASON_LINE_INVALID)
    parts = _split_combo(s.side)
    if parts is None:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    r_side, ou_side = parts
    if r_side not in _1X2_TOKENS or ou_side not in _OU_TOKENS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return Verdict(
        verdict=_combine_legs(
            _cmp_1x2_leg(score, r_side), _cmp_ou(score.total, line, ou_side)
        )
    )


def _settle_double_chance_and_total_goals(
    score: TeamScore, s: SideAndLine
) -> Verdict:
    """Both legs read the basis-selected score. Match-level `547`,
    1h `900042` (sportybet-private id block for the DC/OU tokens),
    2h `900043`."""
    if len(s.line_values) != 1:
        return _unsettleable(REASON_LINE_INVALID)
    line = _decimal_line(s.line_values[0])
    if line is None:
        return _unsettleable(REASON_LINE_INVALID)
    parts = _split_combo(s.side)
    if parts is None:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    dc_side, ou_side = parts
    if dc_side not in _DC_TOKENS or ou_side not in _OU_TOKENS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return Verdict(
        verdict=_combine_legs(
            _cmp_dc_leg(score, dc_side), _cmp_ou(score.total, line, ou_side)
        )
    )


def _settle_total_goals_and_btts(score: TeamScore, s: SideAndLine) -> Verdict:
    """Both legs read the basis-selected score. Match-level `36`."""
    if len(s.line_values) != 1:
        return _unsettleable(REASON_LINE_INVALID)
    line = _decimal_line(s.line_values[0])
    if line is None:
        return _unsettleable(REASON_LINE_INVALID)
    parts = _split_combo(s.side)
    if parts is None:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    ou_side, btts_side = parts
    if ou_side not in _OU_TOKENS or btts_side not in _YES_NO_TOKENS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return Verdict(
        verdict=_combine_legs(
            _cmp_ou(score.total, line, ou_side), _cmp_btts(score, btts_side)
        )
    )


def _settle_htft_result_pair(s: SideAndLine) -> tuple[str, str] | Verdict:
    """Parse the HT/FT part of a combination side into (ht, ft) 1X2 tokens.
    Returns a `Verdict(unsettleable)` on a malformed side, or the pair on
    success. Shared helper — HT/FT combinations have three families and
    parsing the leg once keeps the D26(c) per-leg component note beside
    each family's handler."""
    parts = _split_combo(s.side)
    if parts is None:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    ht_ft_part, _other = parts
    if "_" not in ht_ft_part:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    ht_leg, _sep, ft_leg = ht_ft_part.partition("_")
    if ht_leg not in _1X2_TOKENS or ft_leg not in _1X2_TOKENS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return (ht_leg, ft_leg)


def _settle_halftime_fulltime_and_total_goals(
    scores: PeriodResolvedScores, s: SideAndLine
) -> Verdict:
    """`818`. Time basis is `regular` (D26(c)). Per D18 components:
    HT leg reads `h1`; FT leg reads `reg`; total leg reads `reg.total`.
    A fixture where h1's 1X2 outcome differs from reg's proves each leg
    reads its own component (rather than being forced onto one of them);
    the tests exercise exactly that fixture, per the task fence."""
    if len(s.line_values) != 1:
        return _unsettleable(REASON_LINE_INVALID)
    line = _decimal_line(s.line_values[0])
    if line is None:
        return _unsettleable(REASON_LINE_INVALID)
    pair = _settle_htft_result_pair(s)
    if isinstance(pair, Verdict):
        return pair
    if scores.h1 is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    assert scores.reg is not None
    ht_side, ft_side = pair
    parts = _split_combo(s.side)
    assert parts is not None
    _htft_part, ou_side = parts
    if ou_side not in _OU_TOKENS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    ht = _cmp_1x2(scores.h1.home, scores.h1.away)
    ft = _cmp_1x2(scores.reg.home, scores.reg.away)
    htft_leg = VERDICT_WON if (ht_side == ht and ft_side == ft) else VERDICT_LOST
    ou_leg = _cmp_ou(scores.reg.total, line, ou_side)
    return Verdict(verdict=_combine_legs(htft_leg, ou_leg))


def _settle_halftime_fulltime_and_first_half_total_goals(
    scores: PeriodResolvedScores, s: SideAndLine
) -> Verdict:
    """`819`. Time basis is `regular` (D26(c), the worked case for the
    clarification). Per D18 components: HT leg reads `h1`; FT leg reads
    `reg`; total leg reads `h1.total` — a **DIFFERENT** component from
    the `total` leg of `818`. This is the D26(c) hazard the task fence
    calls out: a naive engine that forced both legs onto `reg` would
    silently mis-settle every fixture where reg.total ≠ h1.total. The
    tests exercise such a fixture."""
    if len(s.line_values) != 1:
        return _unsettleable(REASON_LINE_INVALID)
    line = _decimal_line(s.line_values[0])
    if line is None:
        return _unsettleable(REASON_LINE_INVALID)
    pair = _settle_htft_result_pair(s)
    if isinstance(pair, Verdict):
        return pair
    if scores.h1 is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    assert scores.reg is not None
    ht_side, ft_side = pair
    parts = _split_combo(s.side)
    assert parts is not None
    _htft_part, ou_side = parts
    if ou_side not in _OU_TOKENS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    ht = _cmp_1x2(scores.h1.home, scores.h1.away)
    ft = _cmp_1x2(scores.reg.home, scores.reg.away)
    htft_leg = VERDICT_WON if (ht_side == ht and ft_side == ft) else VERDICT_LOST
    ou_leg = _cmp_ou(scores.h1.total, line, ou_side)
    return Verdict(verdict=_combine_legs(htft_leg, ou_leg))


#: Enumerated exact-goals buckets for the HT/FT & exact-goals family
#: (`820`). Betradar publishes `0`, `1`, `2`, `3`, `4`, and `5+` — the
#: last is "5 or more", the natural reading of the canonical `N+` token
#: this vocabulary uses everywhere else (multigoals, goal_bounds,
#: goal_range's structured ids). Match-level total goals; a `line`-free
#: side. Enumerated as tokens rather than as raw ids because the outcome
#: map has already canonicalised them (D12).
_HTFT_EXACT_BUCKETS: Final = frozenset({"0", "1", "2", "3", "4", "5+"})


def _exact_goals_bucket_matches(total: int, bucket: str) -> bool | None:
    """True/False whether `total` falls in a canonical exact-goals bucket.
    None if the bucket token is not one this family knows."""
    if bucket not in _HTFT_EXACT_BUCKETS:
        return None
    if bucket.endswith("+"):
        return total >= int(bucket[:-1])
    return total == int(bucket)


def _settle_halftime_fulltime_and_exact_goals(
    scores: PeriodResolvedScores, s: SideAndLine
) -> Verdict:
    """`820`. Time basis is `regular` (D26(c)). Per D18 components:
    HT leg reads `h1`; FT leg reads `reg`; exact-goals leg reads
    `reg.total` (the match-level total)."""
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    pair = _settle_htft_result_pair(s)
    if isinstance(pair, Verdict):
        return pair
    if scores.h1 is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    assert scores.reg is not None
    ht_side, ft_side = pair
    parts = _split_combo(s.side)
    assert parts is not None
    _htft_part, exact_bucket = parts
    matched = _exact_goals_bucket_matches(scores.reg.total, exact_bucket)
    if matched is None:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    ht = _cmp_1x2(scores.h1.home, scores.h1.away)
    ft = _cmp_1x2(scores.reg.home, scores.reg.away)
    htft_leg = VERDICT_WON if (ht_side == ht and ft_side == ft) else VERDICT_LOST
    exact_leg = VERDICT_WON if matched else VERDICT_LOST
    return Verdict(verdict=_combine_legs(htft_leg, exact_leg))


#: The set of enumerated (HT, FT) score buckets in
#: `SIDES_HALFTIME_FULLTIME_CORRECT_SCORE`. A `4+` token on either side of
#: the pair reads as "the score's max component is ≥ 4" — the standard
#: betradar `N+` bucket reading, canonical since it comes off the map.
_HTFT_CORRECT_SCORE_BUCKETS: Final = frozenset(
    {"0:0", "0:1", "0:2", "0:3", "1:0", "1:1", "1:2", "2:0", "2:1", "3:0", "4+"}
)


def _htft_score_bucket_matches(score: TeamScore, bucket: str) -> bool | None:
    """True/False whether `score` matches the HT/FT correct-score bucket.
    None if the token is not one of the enumerated buckets."""
    if bucket == "4+":
        return score.home >= 4 or score.away >= 4
    if ":" not in bucket:
        return None
    left, _, right = bucket.partition(":")
    try:
        h, a = int(left), int(right)
    except ValueError:
        return None
    return score.home == h and score.away == a


def _settle_halftime_fulltime_correct_score(
    scores: PeriodResolvedScores, s: SideAndLine
) -> Verdict:
    """`46`. Time basis is `regular` (D26(c) — same reasoning as `47`
    `halftime_fulltime` and `818`). Per D18 components: HT leg reads
    `h1`, FT leg reads `reg`, each as a full `(home, away)` score
    (not just a 1X2 outcome).
    Side format is `<ht_bucket>_<ft_bucket>` where each bucket is either
    `N:M` (exact score) or `4+` (the score's max component ≥ 4). Sides
    like `0:0_0:0` (both HT and FT are 0:0) are the common shape; only
    `4+_4+` has `4+` at HT."""
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    if scores.h1 is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    assert scores.reg is not None
    # Split on the FIRST `_` — the two buckets are `N:M` or `4+`, neither
    # of which contains an underscore, so the first split is unambiguous.
    if "_" not in s.side:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    ht_bucket, _sep, ft_bucket = s.side.partition("_")
    if ht_bucket not in _HTFT_CORRECT_SCORE_BUCKETS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    if ft_bucket not in _HTFT_CORRECT_SCORE_BUCKETS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    ht_ok = _htft_score_bucket_matches(scores.h1, ht_bucket)
    ft_ok = _htft_score_bucket_matches(scores.reg, ft_bucket)
    # Both bucket lookups are known-good after the enum check above.
    assert ht_ok is not None and ft_ok is not None
    return _won_or_lost(ht_ok and ft_ok)


def _settle_double_chance_and_half_btts(
    half: str, scores: PeriodResolvedScores, s: SideAndLine
) -> Verdict:
    """`540` (`half='h1'`) and `541` (`half='h2'`). Time basis is
    `regular` (D26(c)). Per D18 components: DC leg reads `reg` (needs
    the WHOLE match); BTTS leg reads the named half — h1 for `540`, h2
    for `541`. This is the exact hazard D26(c) exists to name: `1h` (or
    `2h`) would force BOTH legs onto the half score and mis-settle the
    match-scoped DC leg."""
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    parts = _split_combo(s.side)
    if parts is None:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    dc_side, btts_side = parts
    if dc_side not in _DC_TOKENS or btts_side not in _YES_NO_TOKENS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    half_score = scores.h1 if half == "h1" else scores.h2
    if half_score is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    assert scores.reg is not None
    dc_leg = _cmp_dc_leg(scores.reg, dc_side)
    btts_leg = _cmp_btts(half_score, btts_side)
    return Verdict(verdict=_combine_legs(dc_leg, btts_leg))


def _settle_first_and_second_half_btts(
    scores: PeriodResolvedScores, s: SideAndLine
) -> Verdict:
    """`55`. Time basis is `regular` — one basis, two half components
    (D26(c) again). Per D18 components: 1st-half leg reads `h1`; 2nd-half
    leg reads `h2`. Side format is `<h1_btts>_<h2_btts>` where each is
    `yes` or `no` — e.g. `yes_no` = h1 both scored, h2 they did not.
    Sides come from the outcome map's `SIDES_FIRST_AND_SECOND_HALF_BTTS`
    block (`806`/`808`/`810`/`812`)."""
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    if scores.h1 is None or scores.h2 is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)
    if "_" not in s.side:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    h1_side, _sep, h2_side = s.side.partition("_")
    if h1_side not in _YES_NO_TOKENS or h2_side not in _YES_NO_TOKENS:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    # Both legs are boolean, so no combo-push case — every input decides.
    h1_ok = _cmp_btts(scores.h1, h1_side)
    h2_ok = _cmp_btts(scores.h2, h2_side)
    return _won_or_lost(h1_ok == VERDICT_WON and h2_ok == VERDICT_WON)


#: `551` "Multiscores" — enumerated home-win / away-win score buckets
#: (from the outcome map's `SIDES_MULTISCORES`). "other_homewin" wins
#: on a home victory whose exact score is NOT in these buckets; same
#: reading for "other_awaywin". The score buckets are the union of every
#: `N:M` token in `SIDES_MULTISCORES`'s enumerated groups; this table
#: mirrors that vocabulary so the "other" catch-all reads coherently.
_MULTISCORES_ENUM_HOMEWINS: Final = frozenset(
    {"1:0", "2:0", "3:0", "4:0", "5:0", "6:0", "2:1", "3:1", "4:1", "3:2", "4:2", "4:3", "5:1"}
)
_MULTISCORES_ENUM_AWAYWINS: Final = frozenset(
    {"0:1", "0:2", "0:3", "0:4", "0:5", "0:6", "1:2", "1:3", "1:4", "2:3", "2:4", "3:4", "1:5"}
)


def _settle_multiscores(score: TeamScore, s: SideAndLine) -> Verdict:
    """`551`. Time basis is `regular` (map says so). The side is a token
    from `SIDES_MULTISCORES`: a `_or_`-joined list of specific scores
    (e.g. `1:0_2:0_or_3:0`), or one of the three catch-alls
    `other_homewin` / `other_awaywin` / `draw`.
    "draw" wins on any draw; "other_homewin" wins on a home victory
    whose score is not in the enumerated home-win buckets; symmetric for
    "other_awaywin". This is the natural reading of the "other" catch-all
    — the same rule the enumerated `correct_score` uses for its own
    `other` — and the map's own note calls these out as **real catch-all
    outcomes**, not shrugs."""
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    actual = f"{score.home}:{score.away}"
    if s.side == "draw":
        return _won_or_lost(score.home == score.away)
    if s.side == "other_homewin":
        if score.home <= score.away:
            return Verdict(verdict=VERDICT_LOST)
        return _won_or_lost(actual not in _MULTISCORES_ENUM_HOMEWINS)
    if s.side == "other_awaywin":
        if score.away <= score.home:
            return Verdict(verdict=VERDICT_LOST)
        return _won_or_lost(actual not in _MULTISCORES_ENUM_AWAYWINS)
    # A list of specific scores joined by `_` between the first N-1 and
    # `_or_` before the last one — the natural-language rendering the
    # outcome map preserves ("1:0, 2:0, or 3:0" → "1:0_2:0_or_3:0"). Both
    # separators are collapsed to `_` before splitting; each part must
    # be an `N:M` token or the side is unknown (this family produces no
    # `4+` bucket, unlike halftime_fulltime_correct_score).
    if "_or_" not in s.side:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    parts = s.side.replace("_or_", "_").split("_")
    for part in parts:
        if ":" not in part:
            return _unsettleable(REASON_UNKNOWN_SIDE)
        left, _, right = part.partition(":")
        try:
            int(left)
            int(right)
        except ValueError:
            return _unsettleable(REASON_UNKNOWN_SIDE)
    return _won_or_lost(actual in parts)


def _goal_bounds_bucket(goals: int, side: str) -> Verdict:
    """Settle a goal-bounds bucket. Sides come from `SIDES_GOAL_BOUNDS`
    / `SIDES_GOAL_BOUNDS_TEAM`:
        `N`      (bare integer)     → total == N
        `N+`     (open-ended)       → total >= N
        `N-M`    (closed range)     → N <= total <= M
        `N-M+`   (open-ended range) → total >= N   (M+ is unbounded above)
    Distinct from `_multigoals_bucket` because of the `N-M+` form (not
    used by the multigoals block); a family-specific parser rather than a
    silent widening of the multigoals rule, so a future divergence in
    either vocabulary does not accidentally cross-contaminate."""
    if side.endswith("+"):
        if "-" in side:
            left, _, right = side.partition("-")
            # right is "M+" — the upper bound is unbounded, so this is a
            # `>= N` bucket regardless of the M numeric value.
            if not right.endswith("+"):
                return _unsettleable(REASON_UNKNOWN_SIDE)
            try:
                low = int(left)
                int(right[:-1])  # ensure it parses as an integer
            except ValueError:
                return _unsettleable(REASON_UNKNOWN_SIDE)
            return _won_or_lost(goals >= low)
        try:
            threshold = int(side[:-1])
        except ValueError:
            return _unsettleable(REASON_UNKNOWN_SIDE)
        return _won_or_lost(goals >= threshold)
    if "-" in side:
        left, _, right = side.partition("-")
        try:
            low, high = int(left), int(right)
        except ValueError:
            return _unsettleable(REASON_UNKNOWN_SIDE)
        return _won_or_lost(low <= goals <= high)
    try:
        exact = int(side)
    except ValueError:
        return _unsettleable(REASON_UNKNOWN_SIDE)
    return _won_or_lost(goals == exact)


def _settle_goal_bounds(family: str, score: TeamScore, s: SideAndLine) -> Verdict:
    """`450001` (match totals), `450002`/`450003` (home/away team totals),
    `810001` (1st-half match totals; the sportybet-private half twin
    0005e added under its own family because the outcome id block reuses
    the team-total values at different meanings). Per D18 components:
    match reads the basis-selected `total`, home reads `home`, away
    reads `away`, first_half is called under `1h` basis so the selected
    score is already `h1` — meaning `family=goal_bounds_first_half`
    reads the h1 match total, exactly as its label says."""
    if s.line_values:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)
    if family == "goal_bounds_home":
        goals = score.home
    elif family == "goal_bounds_away":
        goals = score.away
    else:
        # `goal_bounds` (match) and `goal_bounds_first_half` (h1 basis,
        # score already selected by the basis) both read the total.
        goals = score.total
    return _goal_bounds_bucket(goals, s.side)


# --- the entrypoint ----------------------------------------------------------


def settle(
    scores: PeriodResolvedScores,
    status: str,
    market_family: str,
    period: str,
    time_basis: str,
    side_or_line: str,
) -> Verdict:
    """Score + market -> verdict. The one public function this engine offers.

    Signature order matches the shape a caller would build from a joined row:
    the score set (from the result fact) first, then the fields that come off
    `dim_market` (`market_family`, `period`, `time_basis`), then the outcome's
    `side_or_line`. Nothing here is an odds column, an id or a foreign key —
    D10's odds-freeness and D12's "identity is ids" both hold by signature.

    `period` is passed alongside `time_basis` because a few families
    (`correct_score`) admit different enumerations at match/1h/2h that
    `time_basis` alone does not disambiguate. `time_basis` remains the
    authority for *which score* to read.
    """
    # D21 first: an unknown status is unsettleable (never void), a non-settlable
    # known status voids everything. Void is a fact about the event; a market's
    # basis or side is irrelevant once the event did not complete.
    if status not in KNOWN_STATUSES:
        return _unsettleable(REASON_UNKNOWN_STATUS)
    if status not in SETTLABLE_STATUSES:
        return Verdict(verdict=VERDICT_VOID)

    # Player props (D13) are a distinct backlog — refuse with their own reason
    # before the family-not-implemented check so the two lists stay separate.
    if market_family in _PLAYER_PROP_FAMILIES:
        return _unsettleable(REASON_PLAYER_PROP_NOT_IMPLEMENTED)

    if market_family not in TRANCHE_FAMILIES:
        return _unsettleable(REASON_FAMILY_NOT_IMPLEMENTED)

    # Time basis: `other` is a positive refusal (D26(b)); an unknown token is
    # a separate refusal so a typo does not read as `other`.
    if time_basis == BASIS_OTHER:
        return _unsettleable(REASON_TIME_BASIS_OTHER)
    if time_basis not in {BASIS_REGULAR, BASIS_FULL, BASIS_1H, BASIS_2H}:
        return _unsettleable(REASON_TIME_BASIS_UNKNOWN)

    parsed = parse_side_or_line(side_or_line)
    if parsed is None:
        return _unsettleable(REASON_UNPARSEABLE_SIDE_OR_LINE)

    score = _select_score(scores, time_basis)
    # A handful of half-reading match-level families (halftime_fulltime,
    # highest_scoring_half, both_halves_*, score_in_both_halves_*,
    # win_both_halves_*, win_either_half_*) need components beyond the basis's
    # own — those checks live inside each handler, because "the basis is
    # present but the half is not" is a different missing_score_component
    # finding from "the basis itself is not present".
    if score is None:
        return _unsettleable(REASON_MISSING_SCORE_COMPONENT)

    # Dispatch. One conditional table, one handler per family; the shape
    # matches `betradar_market_map.py`'s reviewable-data-table shape one
    # grain down. A family in TRANCHE_FAMILIES with no dispatch here is a
    # coding error (`test_every_tranche_family_has_a_handler` catches it).
    if market_family == "1x2":
        return _settle_1x2(score, parsed)
    if market_family == "double_chance":
        return _settle_double_chance(score, parsed)
    if market_family in {"draw_no_bet", "home_no_bet", "away_no_bet"}:
        return _settle_dnb_variants(market_family, score, parsed)
    if market_family in {"total_goals", "total_goals_home", "total_goals_away"}:
        return _settle_total_goals(market_family, score, parsed)
    if market_family == "btts":
        return _settle_btts(score, parsed)
    if market_family == "correct_score":
        return _settle_correct_score(period, score, parsed)
    if market_family == "halftime_fulltime":
        return _settle_halftime_fulltime(scores, parsed)
    if market_family in {"odd_even_goals", "odd_even_home", "odd_even_away"}:
        return _settle_odd_even(market_family, score, parsed)
    if market_family == "handicap":
        return _settle_handicap(score, parsed)
    if market_family == "asian_handicap":
        return _settle_asian_handicap(score, parsed)
    if market_family == "teams_to_score":
        return _settle_teams_to_score(score, parsed)
    if market_family in {"clean_sheet_home", "clean_sheet_away"}:
        return _settle_clean_sheet(market_family, score, parsed)
    if market_family in {"win_to_nil_home", "win_to_nil_away"}:
        return _settle_win_to_nil(market_family, score, parsed)
    if market_family == "highest_scoring_half":
        return _settle_highest_scoring_half("match", scores, parsed)
    if market_family == "highest_scoring_half_home":
        return _settle_highest_scoring_half("home", scores, parsed)
    if market_family == "highest_scoring_half_away":
        return _settle_highest_scoring_half("away", scores, parsed)
    if market_family == "both_halves_over":
        return _settle_both_halves_over(scores, side_yes=True, s=parsed)
    if market_family == "both_halves_under":
        return _settle_both_halves_over(scores, side_yes=False, s=parsed)
    if market_family == "score_in_both_halves_home":
        return _settle_score_in_both_halves("home", scores, parsed)
    if market_family == "score_in_both_halves_away":
        return _settle_score_in_both_halves("away", scores, parsed)
    if market_family == "win_both_halves_home":
        return _settle_win_both_halves("home", scores, parsed)
    if market_family == "win_both_halves_away":
        return _settle_win_both_halves("away", scores, parsed)
    if market_family == "win_either_half_home":
        return _settle_win_either_half("home", scores, parsed)
    if market_family == "win_either_half_away":
        return _settle_win_either_half("away", scores, parsed)
    if market_family in {"multigoals", "multigoals_home", "multigoals_away"}:
        return _settle_multigoals(market_family, score, parsed)
    # --- 0009c dispatch: combination and bucket-vocabulary families ---------
    if market_family == "1x2_and_btts":
        return _settle_1x2_and_btts(score, parsed)
    if market_family == "double_chance_and_btts":
        return _settle_double_chance_and_btts(score, parsed)
    if market_family == "1x2_and_total_goals":
        return _settle_1x2_and_total_goals(score, parsed)
    if market_family == "double_chance_and_total_goals":
        return _settle_double_chance_and_total_goals(score, parsed)
    if market_family == "total_goals_and_btts":
        return _settle_total_goals_and_btts(score, parsed)
    if market_family == "halftime_fulltime_and_total_goals":
        return _settle_halftime_fulltime_and_total_goals(scores, parsed)
    if market_family == "halftime_fulltime_and_first_half_total_goals":
        return _settle_halftime_fulltime_and_first_half_total_goals(scores, parsed)
    if market_family == "halftime_fulltime_and_exact_goals":
        return _settle_halftime_fulltime_and_exact_goals(scores, parsed)
    if market_family == "halftime_fulltime_correct_score":
        return _settle_halftime_fulltime_correct_score(scores, parsed)
    if market_family == "double_chance_and_first_half_btts":
        return _settle_double_chance_and_half_btts("h1", scores, parsed)
    if market_family == "double_chance_and_second_half_btts":
        return _settle_double_chance_and_half_btts("h2", scores, parsed)
    if market_family == "first_and_second_half_btts":
        return _settle_first_and_second_half_btts(scores, parsed)
    if market_family == "multiscores":
        return _settle_multiscores(score, parsed)
    if market_family in {
        "goal_bounds", "goal_bounds_home", "goal_bounds_away",
        "goal_bounds_first_half",
    }:
        return _settle_goal_bounds(market_family, score, parsed)

    # Unreachable while TRANCHE_FAMILIES matches the dispatch above. Kept as
    # a positive refusal — never a lost fallback — so an edit that adds a
    # family to TRANCHE_FAMILIES without wiring it up here still returns
    # `unsettleable`, not a fabricated verdict.
    return _unsettleable(REASON_FAMILY_NOT_IMPLEMENTED)  # pragma: no cover
