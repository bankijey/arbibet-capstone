"""The betradar market id → `(market_family, period, time_basis)` decisions.

**This module is data, and it is meant to be read.** When a settlement looks
wrong two years from now, this table is the one place that says what silver
believed a market id meant and why. So every row carries the label the evidence
recorded for it, the D26 rule that fixed its time basis, and nothing that a
reader would have to go and re-derive.

Evidence: `docs/betradar-taxonomy-survey.md` (task 0005a) and the recorded
payloads under `tests/fixtures/market_payloads/`. That document is the single
source of truth for every figure; this module quotes **labels**, which is
evidence about meaning, not statistics about volume.

## What the natural key is, and what it therefore excludes

`dim_market`'s natural key is `(market_family, period)` — not the betradar
market id. Ids `18` (Over/Under), `68` (1st Half - Over/Under) and `90` (2nd
Half - Over/Under) are **one family at three periods**, which is exactly what
that key expresses, and several ids may legitimately land on one row.

What must NOT appear in a family, and where it lives instead:

- **the line** (2.5, -0.5, 0:1) — `dim_outcome.side_or_line` (D5, and
  `0001_dimensions.sql` says so on the table). Market `58` is labelled "Both
  Halves Over 1.5" by the book; the family here is `both_halves_over` and the
  1.5 is 0005d's problem.
- **the bookmaker** — `outcome_xref.bookmaker_key` (D12). No family below is a
  book's word for anything; several are deliberately *not* the label any book
  publishes.
- **the period** — its own column, so `handicap` appears three times (match,
  1h, 2h) rather than as three families.

## The time-basis rule (D19 + D26), applied not re-decided

A row does not store `time_basis` directly. It stores **which D26 rule fired**,
and `time_basis` is derived from that (`TIME_BASIS_BY_RULE`). The point is
auditability: "regular because nothing said otherwise" and "regular because the
rules text said regular time" are the same value but different claims, and a
table that only recorded the value would lose the difference.

1. `RULE_FIRST_HALF` → `period = 1h`, `time_basis = 1h`
2. `RULE_SECOND_HALF` → `period = 2h`, `time_basis = 2h`
3. `RULE_OVERTIME_INCLUDED` → `time_basis = full`
4. `RULE_OUTSIDE_THE_FOUR` → `time_basis = other` (D26(b)) — the market settles
   on something none of D19's four bases describes. Minute-range markets
   (`900313`, `900069`, `60180`, `100`, `101`, `105`) and early-payout markets
   (`60200`) live here. Real odds, real rows, **outside** D20's v1 settlement
   scope: the calculator must refuse them (`unsettleable`, D10) rather than
   guess.
5. `RULE_NO_PERIOD_SIGNAL` → `time_basis = regular` (D26(a)). Absence of a
   period signal is a **positive answer**, not an unknown — betradar labels its
   period markets explicitly (`68` is "1st Half - Over/Under" against `18`
   "Over/Under"), so a bare label means the whole of regular time.

An id that is **not in this table** is not guessed at. The resolver counts it,
logs it, and writes no row (see `betradar_markets.py`) — `other` is a statement
that the basis is known to be none of the four, never a shrug.

### One application note that a reader will otherwise trip over

Rules 1 and 2 ask whether the market settles **wholly** on that half, because
that is what a `1h`/`2h` basis means to the calculator: read only the h1 (or
h2) score. A half mentioned in *one leg* of a match-level combination market is
not that signal:

- `819` "Halftime/Fulltime & 1st Half Over/Under" carries "1st Half" wording,
  but its halftime/fulltime leg needs the **regular-time** result. `1h` would
  mis-settle that leg silently. Both of its legs resolve inside regular time,
  and D18 stores `{h1, h2, reg, …}` per team, so `regular` is the basis that
  gives the calculator every score the market actually needs. Rule 5.
- The same reading keeps `47` (Halftime/Fulltime), `818`, and the both-halves
  markets (`48`–`59`) on `regular`. It has to: D20 puts HT/FT **inside** v1
  settlement scope while D26(b) puts every `other` outside it, so classifying
  HT/FT as `other` would quietly drop a market D20 requires to settle.

### Where books label the same id differently, the id wins (D26(a))

Market `30` is "Teams to Score" to sportybet and "Which Team To Score" to
ilotbet; market `19` is "Home Total" to ilotbet and "Atletico Madrid
Over/Under" to sportybet, which substitutes the team name. One row is assigned
here, per id, once. The resolver logs the disagreement; it never averages it or
lets the last book read win.

## Player props (D13)

`is_player_prop` is **declared** here for the prop markets the evidence names,
and **detected structurally** by the resolver off `sr:player:` /
`pre:playerprops:` outcome ids and the `player` specifier key — labels
localise, ids do not. Declaring it as well gives the flag a reviewable home and
lets a mismatch between claim and payload be logged instead of silently
absorbed. D13 defers prop **settlement** to v2; it does not defer their
existence, so they resolve normally here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# --- dim_market vocabularies -------------------------------------------------
#
# `time_basis` values, exactly `ck_dim_market_time_basis` after
# `migrations/0007_time_basis_other.sql` (D19's four + D26(b)'s fifth).
TIME_BASIS_REGULAR: Final = "regular"
TIME_BASIS_FULL: Final = "full"
TIME_BASIS_1H: Final = "1h"
TIME_BASIS_2H: Final = "2h"
TIME_BASIS_OTHER: Final = "other"

TIME_BASES: Final = (
    TIME_BASIS_REGULAR,
    TIME_BASIS_FULL,
    TIME_BASIS_1H,
    TIME_BASIS_2H,
    TIME_BASIS_OTHER,
)

#: `dim_market.period`. Extend only if the data forces it — and say so here.
PERIOD_MATCH: Final = "match"
PERIOD_1H: Final = "1h"
PERIOD_2H: Final = "2h"

PERIODS: Final = (PERIOD_MATCH, PERIOD_1H, PERIOD_2H)

# --- D26's five rules --------------------------------------------------------
#
# The rule token is what a row stores; the basis is derived. Named after the
# *evidence* that fires the rule, not after the value it yields, so a row reads
# as a claim about the market rather than as an assertion about the column.
RULE_FIRST_HALF: Final = "label_says_first_half"
RULE_SECOND_HALF: Final = "label_says_second_half"
RULE_OVERTIME_INCLUDED: Final = "label_says_overtime_included"
RULE_OUTSIDE_THE_FOUR: Final = "settles_outside_the_four_bases"
RULE_NO_PERIOD_SIGNAL: Final = "no_period_signal"

TIME_BASIS_BY_RULE: Final[dict[str, str]] = {
    RULE_FIRST_HALF: TIME_BASIS_1H,
    RULE_SECOND_HALF: TIME_BASIS_2H,
    RULE_OVERTIME_INCLUDED: TIME_BASIS_FULL,
    RULE_OUTSIDE_THE_FOUR: TIME_BASIS_OTHER,
    RULE_NO_PERIOD_SIGNAL: TIME_BASIS_REGULAR,
}

#: The period a rule implies where it implies one. Rules 3-5 say nothing about
#: the period, so a row using them states it (almost always `match`).
PERIOD_BY_RULE: Final[dict[str, str]] = {
    RULE_FIRST_HALF: PERIOD_1H,
    RULE_SECOND_HALF: PERIOD_2H,
}


def time_basis_for_rule(rule: str) -> str:
    """D26's rule → the `dim_market.time_basis` it yields."""
    try:
        return TIME_BASIS_BY_RULE[rule]
    except KeyError:  # pragma: no cover - a typo in the table, not a data case
        raise ValueError(f"unknown time-basis rule {rule!r}") from None


@dataclass(frozen=True, slots=True)
class MarketMapping:
    """One betradar market id's resolved identity, with its evidence.

    `market_id` is the **bare** betradar id (`"18"`), never the specifier-joined
    form (`"18;2.5"`): the line is a lower grain (D5).
    """

    market_id: str
    market_family: str
    period: str
    #: Which D26 rule fixed the basis. `time_basis` is derived, never stored.
    rule: str
    #: A canonical, book-neutral display name — the fallback for
    #: `dim_market.name` when a run observes no label at all.
    name: str
    #: The label(s) the evidence recorded, so a reader can check the decision
    #: without opening the survey. Verbatim, including a book's own casing.
    evidence: str
    is_player_prop: bool = False
    #: Refuse the observed label for `dim_market.name` and keep `name` above.
    #: Set where a book's label embeds a value that is really a **line** — the
    #: minute window in "1X2 from 1 to 85 minute", the `{from}`/`{!goalnr}`
    #: templates — so `name` would otherwise read as though the market were the
    #: one window this run happened to sample most (0005b verifier, Obs 3). The
    #: window lives in `dim_outcome.side_or_line`, where it belongs.
    pin_name: bool = False

    @property
    def time_basis(self) -> str:
        return time_basis_for_rule(self.rule)

    @property
    def identity(self) -> tuple[str, str]:
        """The `dim_market` natural key this id resolves to."""
        return (self.market_family, self.period)


def _m(
    market_id: str,
    market_family: str,
    period: str,
    rule: str,
    name: str,
    evidence: str,
    *,
    prop: bool = False,
    pin_name: bool = False,
) -> MarketMapping:
    return MarketMapping(
        market_id=market_id,
        market_family=market_family,
        period=period,
        rule=rule,
        name=name,
        evidence=evidence,
        is_player_prop=prop,
        pin_name=pin_name,
    )


# ---------------------------------------------------------------------------
# The table. Ascending by numeric id, so an auditor can find a row the way a
# payload presents it. Grouped by the blocks betradar itself uses:
#
#   1-59       whole-match markets
#   65-92      the explicit half markets (1st half 6x, 2nd half 8x/9x)
#   100-105    minute-interval markets
#   450xxx     sportybet's goal-bounds / excluded-goals family
#   546-549    combination markets (540/541 wholly-regular, 542-545 wholly-half)
#   60xxx      early-payout and goal-streak markets
#   810xxx     sportybet's private half-twins of the 450xxx block (0005e)
#   818-899    halftime/fulltime combinations, to-win markets, player props
#   9000xx     sportybet's minute-range markets, plus its private half-twins
#              of `33`/`547` (900036/900038/900042/900043, 0005e)
#
# Every row's `evidence` is a label from `docs/betradar-taxonomy-survey.md` or
# from a recorded payload fixture. Ids the evidence names but does not label —
# and every msport private-range id (`1000021`, `2000021`, `3000026`,
# `5000002`, …) — are deliberately **absent**: an id with no observed meaning
# gets counted and logged, never a family invented for it.
# ---------------------------------------------------------------------------
_MAPPINGS: Final[tuple[MarketMapping, ...]] = (
    # --- whole-match markets -------------------------------------------------
    _m("1", "1x2", PERIOD_MATCH, RULE_NO_PERIOD_SIGNAL, "1X2", "1X2; 1x2"),
    _m("8", "next_goal", PERIOD_MATCH, RULE_NO_PERIOD_SIGNAL, "Next Goal", "Next Goal"),
    # `9` "Last Goal" (task 0007b) — sibling of `8` `next_goal` at the same
    # grain, same outcome ids (`6`/`7`/`8` home/none/away). Its own family
    # because "last goal" and "next goal" settle differently: `next_goal` is
    # who scores the Xth goal (asked before it happens), `last_goal` is who
    # scores the *final* goal (only settleable at FT). Regular time.
    _m("9", "last_goal", PERIOD_MATCH, RULE_NO_PERIOD_SIGNAL, "Last Goal", "Last Goal"),
    _m(
        "10",
        "double_chance",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Double Chance",
        "Double Chance",
    ),
    _m("11", "draw_no_bet", PERIOD_MATCH, RULE_NO_PERIOD_SIGNAL, "Draw No Bet", "DNB; Draw No Bet"),
    _m("12", "home_no_bet", PERIOD_MATCH, RULE_NO_PERIOD_SIGNAL, "Home No Bet", "HNB"),
    _m("13", "away_no_bet", PERIOD_MATCH, RULE_NO_PERIOD_SIGNAL, "Away No Bet", "ANB"),
    _m(
        "14",
        "handicap",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Handicap (1X2)",
        "Handicap; Handicap 0:1; Handicap(1X2)",
    ),
    _m(
        "15",
        "winning_margin",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Winning Margin",
        "Winning Margin",
    ),
    _m(
        "16",
        "asian_handicap",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Asian Handicap",
        "Asian Handicap; Asian Handicap -0.5; Handicap",
    ),
    _m("18", "total_goals", PERIOD_MATCH, RULE_NO_PERIOD_SIGNAL, "Over/Under", "Over/Under; Total"),
    # 19/20 keep the side in the family on purpose: the *outcomes* of a team
    # total are Over/Under, so "home" is part of what the market is, not the
    # side of a selection within it. That is the D5 line: `side_or_line` is what
    # varies between the outcomes of one market.
    _m(
        "19",
        "total_goals_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home Total",
        "Home O/U; Home Total; Atletico Madrid Over/Under",
    ),
    _m(
        "20",
        "total_goals_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away Total",
        "Away O/U; Away Total; Southampton Over/Under",
    ),
    _m(
        "21",
        "exact_goals",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Exact Goals",
        "Exact goals; Exact Goals",
    ),
    _m(
        "23",
        "exact_goals_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home Exact Goals",
        "Home Exact goals",
    ),
    _m(
        "24",
        "exact_goals_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away Exact Goals",
        "Away Exact goals",
    ),
    # The one genuine cross-book outcome-id disagreement the survey found
    # (`sr:goal_range:*` vs `sr:point_range:*`). That is an *outcome* problem and
    # belongs to 0005d; the market identity is the same market either way.
    _m("25", "goal_range", PERIOD_MATCH, RULE_NO_PERIOD_SIGNAL, "Goal Range", "Goal Range"),
    _m("26", "odd_even_goals", PERIOD_MATCH, RULE_NO_PERIOD_SIGNAL, "Odd/Even", "Odd/Even"),
    # `27`/`28` (task 0007b) — Home/Away Team Odd/Even. Each their own family,
    # not periods of `odd_even_goals`, because the *scope* differs (whole-match
    # goals vs one team's goals) — the same D5 line `19`/`20` team totals
    # follow against `18` match total. Same outcome ids (`70`/`72`), no
    # collision because they land under different family rows.
    _m(
        "27",
        "odd_even_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home Odd/Even",
        "Home Team Odd/Even; Home Odd/Even; Odd/Even Home",
    ),
    _m(
        "28",
        "odd_even_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away Odd/Even",
        "Away Team Odd/Even; Away Odd/Even; Odd/Even Away",
    ),
    _m(
        "29",
        "btts",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Both Teams To Score",
        "GG/NG; GG|NG",
    ),
    _m(
        "30",
        "teams_to_score",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Which Team To Score",
        "Teams to Score; Which Team To Score",
    ),
    _m(
        "31",
        "clean_sheet_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home Clean Sheet",
        "Home clean sheet; Home Clean Sheet",
    ),
    _m(
        "32",
        "clean_sheet_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away Clean Sheet",
        "Away clean sheet; Away Clean Sheet",
    ),
    _m(
        "33",
        "win_to_nil_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home Win To Nil",
        "Home Win To Nil",
    ),
    # `34` "Away Team to Win to Nil" (task 0007b) — mirrors `33` at the same
    # grain, same `SIDES_YES_NO` ids (`74`/`76`). Observed live in all three
    # books; no sportybet private half-twin observed here, so `win_to_nil_away`
    # does not import `SIDES_YES_NO_SPORTYBET` the way `win_to_nil_home` did
    # for `900037`/`900052` — a family absorbs a private id block only when
    # that id block is actually seen.
    _m(
        "34",
        "win_to_nil_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away Win To Nil",
        "Away Team to Win to Nil; Away Win To Nil; Away win to nil",
    ),
    _m(
        "35",
        "1x2_and_btts",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "1X2 & Both Teams To Score",
        "1x2 & both teams to score; 1X2 & Both Teams To Score",
    ),
    _m(
        "36",
        "total_goals_and_btts",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Total & Both Teams To Score",
        "Total & Both Teams To Score",
    ),
    _m(
        "37",
        "1x2_and_total_goals",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "1X2 & Total",
        "1X2 & Over/Under 2.5; 1X2 & Total; 1x2 & O/U",
    ),
    # 38-40: player props (D13). `goalnr` picks which goal for 38, so the family
    # is the neutral "Xth" sportybet's own `name` field uses — "1st Goalscorer"
    # is the specifier rendered into a label, and would be a line in a family.
    _m(
        "38",
        "goalscorer_nth",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Xth Goalscorer",
        "1st Goalscorer; Goalscorer; 1st goalscorer",
        prop=True,
    ),
    _m(
        "39",
        "goalscorer_last",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Last Goalscorer",
        "Last Goalscorer; Last goalscorer",
        prop=True,
    ),
    _m(
        "40",
        "goalscorer_anytime",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Anytime Goalscorer",
        "Anytime Goalscorer; Anytime goalscorer",
        prop=True,
    ),
    _m(
        "45",
        "correct_score",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Correct Score",
        "Correct score; Correct Score",
    ),
    # `46` "Halftime/Fulltime Correct Score" (task 0007b) — its own family,
    # distinct from `47` `halftime_fulltime` (results only) and `45`
    # `correct_score` (final score only). Regular time: both legs of the label
    # settle inside regular time (D18's h1 and reg components), same reasoning
    # as `47`/`819`. `time_basis = other` would drop a match D20 puts in v1
    # settlement scope.
    _m(
        "46",
        "halftime_fulltime_correct_score",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Halftime/Fulltime Correct Score",
        "Half Time/Full Time Correct Score; Halftime/Fulltime Correct Score; HT/FT correct score",
    ),
    # 47/48-59 all read h1 and/or h2, both of which sit inside regular time —
    # see the application note in this module's docstring for why that is
    # `regular` and not `1h`/`other`.
    _m(
        "47",
        "halftime_fulltime",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Halftime/Fulltime",
        "Halftime/Fulltime",
    ),
    _m(
        "48",
        "win_both_halves_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home Team to Win Both Halves",
        "Home Team to Win Both Halves",
    ),
    _m(
        "49",
        "win_both_halves_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away Team to Win Both Halves",
        "Away Team to Win Both Halves",
    ),
    _m(
        "50",
        "win_either_half_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home Team to Win Either Half",
        "Home Team to Win Either Half",
    ),
    _m(
        "51",
        "win_either_half_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away Team to Win Either Half",
        "Away Team to Win Either Half",
    ),
    _m(
        "52",
        "highest_scoring_half",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Highest Scoring Half",
        "Highest Scoring Half",
    ),
    _m(
        "53",
        "highest_scoring_half_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home Team Highest Scoring Half",
        "Home Team Highest Scoring Half",
    ),
    _m(
        "54",
        "highest_scoring_half_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away Team Highest Scoring Half",
        "Away Team Highest Scoring Half",
    ),
    # `55` "1st/2nd Half Both Teams To Score" (task 0007b) — a single bet on
    # the (1h_btts, 2h_btts) pair, one id per combination (no/no, yes/no,
    # yes/yes, no/yes). Distinct from `75`/`95` (bare per-half BTTS bets):
    # this is one *joint* outcome. Regular time — both halves' BTTS flags are
    # read from D18's period-resolved scores, same reasoning as `47`/`819`.
    _m(
        "55",
        "first_and_second_half_btts",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "1st/2nd Half Both Teams To Score",
        "1st/2nd Half GG/NG; 1st/2nd Half Both Teams To Score; 1st/2nd half both teams to score",
    ),
    _m(
        "56",
        "score_in_both_halves_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home Team to Score In Both Halves",
        "Home Team to Score In Both Halves",
    ),
    _m(
        "57",
        "score_in_both_halves_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away Team to Score In Both Halves",
        "Away Team to Score In Both Halves",
    ),
    # "Both Halves Over 1.5" — the 1.5 is the line and stays off this grain (D5).
    _m(
        "58",
        "both_halves_over",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Both Halves Over",
        "Both Halves Over 1.5",
    ),
    _m(
        "59",
        "both_halves_under",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Both Halves Under",
        "Both Halves Under 1.5",
    ),
    # `184` "Xth Goal & 1X2" (task 0007b) — combination of the goal identity
    # (home/away) with the match 1X2, with `goalnr` specifier picking which
    # goal — same shape as `next_goal` (`8`)/`goalscorer_nth` (`38`), so
    # `goalnr` is a line, not a family. Regular time. `no goal` (`826`) is a
    # legitimate structural side (the Xth goal never happened at all).
    _m(
        "184",
        "xth_goal_and_1x2",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Xth Goal & 1X2",
        "1st Goal & 1X2; 1st goal & 1x2; {!goalnr} goal & 1x2",
    ),
    # `166` "Corners O/U" (task 0014) — total-corners bet, all three books
    # publishing the betradar `12`/`13` over/under id block with a `total`
    # specifier. Its own family — a corner O/U is not a period of
    # `total_goals`, and betradar's team-corner ids (`900300`+) are private
    # to sportybet and stay unmapped this cycle. Regular time.
    _m(
        "166",
        "total_corners",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Total Corners",
        "Corners O/U; Corners - Over/Under; Total Corners",
    ),
    # `854`-`865` "1X2 or Y" combination markets (task 0014) — twelve compound
    # yes/no bets: 1X2 side OR a second leg (O/U at 2.5 for `854`-`859`, BTTS
    # for `860`-`862`, "any clean sheet" for `863`-`865`). Each is its own
    # family per D5 (same rail `27`/`28` team odd/even follow against `26`),
    # because the 1X2 side and the second-leg direction are *market* axes,
    # not outcome axes. Outcomes are all `SIDES_YES_NO` (`74`/`76`), so the
    # id block is shared but each row lands on its own `dim_market` identity.
    # Regular time by D26(c): the 1X2 leg needs the whole regular-time
    # result, and D18 stores every component the second legs need.
    _m(
        "854",
        "home_or_over",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home or Over",
        "Home Team or Over 2.5; Home or over {total}",
    ),
    _m(
        "855",
        "home_or_under",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home or Under",
        "Home Team or Under 2.5; Home or under {total}",
    ),
    _m(
        "856",
        "draw_or_over",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Draw or Over",
        "Draw or Over 2.5; Draw or over 2.5; Draw or over {total}",
    ),
    _m(
        "857",
        "draw_or_under",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Draw or Under",
        "Draw or Under 2.5; Draw or under 2.5; Draw or under {total}",
    ),
    _m(
        "858",
        "away_or_over",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away or Over",
        "Away or Over 2.5; Away or over {total}",
    ),
    _m(
        "859",
        "away_or_under",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away or Under",
        "Away or Under 2.5; Away or under {total}",
    ),
    _m(
        "860",
        "home_or_btts",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home or Both Teams To Score",
        "Home Team or GG; Home Or Both Teams To Score; Home or both teams to score",
    ),
    _m(
        "861",
        "draw_or_btts",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Draw or Both Teams To Score",
        "Draw or GG; Draw Or Both Teams To Score; Draw or both teams to score",
    ),
    _m(
        "862",
        "away_or_btts",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away or Both Teams To Score",
        "Away Team or GG; Away Or Both Teams To Score; Away or both teams to score",
    ),
    _m(
        "863",
        "home_or_any_clean_sheet",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home or Any Clean Sheet",
        "Home Team or Any Clean Sheet; Home Or Any Clean Sheet; Home or any clean sheet",
    ),
    _m(
        "864",
        "draw_or_any_clean_sheet",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Draw or Any Clean Sheet",
        "Draw or Any Clean Sheet; Draw Or Any Clean Sheet; Draw or any clean sheet",
    ),
    _m(
        "865",
        "away_or_any_clean_sheet",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away or Any Clean Sheet",
        "Away Team or Any Clean Sheet; Away Or Any Clean Sheet; Away or any clean sheet",
    ),
    # `1179` "1st Half Result or Match Result" (task 0014) — ilotbet+sportybet.
    # Wins if EITHER the 1st-half result OR the match result matches the
    # picked side. Outcome ids are `1`/`2`/`3` from `SIDES_1X2`. Regular time
    # by D26(c): the match-result leg needs the whole regular-time score,
    # and D18 stores h1 alongside reg so the calculator has both components —
    # `1h` would drop the match leg silently.
    _m(
        "1179",
        "first_half_or_match_result",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "1st Half Result or Match Result",
        "1st Half Result or Match Result",
    ),
    # `770`/`775`/`776`/`777`/`800117` (task 0014) — five sportybet player-prop
    # markets whose outcomes are `pre:playerprops:<event>:<player>[:N]`
    # structured ids, the same shape D13 already defers settlement for on
    # `800097`/`800109` (task 0007b). They resolve identity-first with no
    # `sides` block (every outcome is structured, so `is_structured_outcome_
    # id` catches it and the id lands verbatim in `side_or_line` — which
    # keeps every player distinct under `NULLS NOT DISTINCT` while
    # `player_key` stays NULL, per `betradar_outcome_map.py`'s trap note).
    # The `variant` specifier carries the player id too, but the family
    # declares no `line_keys` — the outcome id already carries the identity,
    # and declaring `variant` as a line would spell the player twice into
    # `side_or_line`. Same rail `800097`/`800109`/`38`/`39`/`40` follow.
    #
    # Four of the five carry "(incl. overtime)" in their label, so their
    # settlement basis is `full` (D19/D26 rule 3); `800117` "Player To Be
    # Booked" carries no period wording, so it stays `regular` (D26(a)).
    # None of them settles in v1 regardless (D13 gates player-prop **market**
    # settlement), but the basis is recorded now rather than fabricated
    # later — same as the shipped `800097` row does.
    _m(
        "770",
        "player_assists_incl_overtime",
        PERIOD_MATCH,
        RULE_OVERTIME_INCLUDED,
        "Player Assists (incl. overtime)",
        "Player assists (incl. overtime)",
        prop=True,
    ),
    _m(
        "775",
        "player_goals_incl_overtime",
        PERIOD_MATCH,
        RULE_OVERTIME_INCLUDED,
        "Player Goals (incl. overtime)",
        "Player goals (incl. overtime)",
        prop=True,
    ),
    _m(
        "776",
        "player_shots_incl_overtime",
        PERIOD_MATCH,
        RULE_OVERTIME_INCLUDED,
        "Player Shots (incl. overtime)",
        "Player shots (incl. overtime)",
        prop=True,
    ),
    _m(
        "777",
        "player_shots_on_goal_incl_overtime",
        PERIOD_MATCH,
        RULE_OVERTIME_INCLUDED,
        "Player Shots on Goal (incl. overtime)",
        "Player shots on goal (incl. overtime)",
        prop=True,
    ),
    _m(
        "800117",
        "player_to_be_booked",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Player To Be Booked",
        "Player To Be Booked",
        prop=True,
    ),
    # --- the explicit half markets ------------------------------------------
    # Same families as their whole-match siblings, one period down. This is the
    # whole reason `(family, period)` is the key rather than the market id.
    #
    # **The half block was completed in 0005d** (0005b verifier, Obs 2). 0005b
    # mapped `71` but not `93`, and `26`/`29`/`31`/`45` but not `94`/`95`/`96`/
    # `98` — an asymmetric tail that resolves a match-level market and then
    # silently drops its half twin, which is worse than resolving neither: a
    # form query would read "over 2.5 in the last 5" as complete while the
    # half markets quietly went missing. Every id added below carries a label
    # observed in the live cohort, in all three books unless noted; none is
    # inferred from an arithmetic offset off the match-level id, because the
    # block has no consistent offset (`18`→`90` is +72, `26`→`94` is +68).
    #
    # Two families keep an asymmetry deliberately: `exact_goals_home` /
    # `exact_goals_away` have 1st-half ids (`72`/`73`) and **no 2nd-half id in
    # the sample**. That is recorded rather than filled — inventing an id is
    # the thing 0005b refused to do for msport's private blocks.
    _m("60", "1x2", PERIOD_1H, RULE_FIRST_HALF, "1st Half - 1X2", "1st Half - 1X2; 1st Half 1X2"),
    _m(
        "62",
        "next_goal",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Next Goal",
        "1st Half - 1st Goal; 1st half - 1st goal",
    ),
    _m(
        "63",
        "double_chance",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Double Chance",
        "1st Half - Double Chance; 1st half - double chance",
    ),
    _m(
        "64",
        "draw_no_bet",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Draw No Bet",
        "1st Half - Draw No Bet",
    ),
    _m(
        "65",
        "handicap",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Handicap (1X2)",
        "1st Half - Handicap; 1st Half - Handicap (1x2); 1st half - handicap",
    ),
    _m(
        "66",
        "asian_handicap",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Asian Handicap",
        "1st Half - Asian Handicap; 1st half - Asian Handicap",
    ),
    _m(
        "68",
        "total_goals",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Over/Under",
        "1st Half - Over/Under; 1st Half Total; 1st half - O/U",
    ),
    _m(
        "69",
        "total_goals_home",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Home Total",
        "1st Half - Home Total; 1st half - Home O/U",
    ),
    _m(
        "70",
        "total_goals_away",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Away Total",
        "1st Half - Away Total; 1st half - Away O/U",
    ),
    _m(
        "71",
        "exact_goals",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Exact Goals",
        "1st Half - Exact Goals (3+); 1st half - exact goals",
    ),
    _m(
        "72",
        "exact_goals_home",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Home Exact Goals",
        "1st Half - Home Exact Goals",
    ),
    _m(
        "73",
        "exact_goals_away",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Away Exact Goals",
        "1st Half - Away Exact Goals",
    ),
    _m(
        "74",
        "odd_even_goals",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Odd/Even",
        "1st Half - Odd/Even; 1st half - odd/even",
    ),
    _m(
        "75",
        "btts",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Both Teams To Score",
        "1st Half - GG/NG; 1st Half GG|NG; 1st half - both teams to score",
    ),
    _m(
        "76",
        "clean_sheet_home",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Home Clean Sheet",
        "1st Half - Home Clean Sheet; 1st Half - Home Team Clean Sheet",
    ),
    _m(
        "77",
        "clean_sheet_away",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Away Clean Sheet",
        "1st Half - Away Clean Sheet",
    ),
    _m(
        "78",
        "1x2_and_btts",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - 1X2 & Both Teams To Score",
        "1st Half - 1X2 & GG/NG; 1st Half - 1x2 & Both Teams To Score",
    ),
    # 79/544 carry a line in the label ("Over/Under 1.5") for the same reason
    # `58` does: the 1.5 is the `total` specifier and stays on `side_or_line`.
    _m(
        "79",
        "1x2_and_total_goals",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - 1X2 & Total",
        "1st Half - 1X2 & Over/Under 1.5; 1st Half - 1x2 & Total",
    ),
    # `81`, not `80`: `80` is "1st Half - Correct Score [0:0]", a *rest of half*
    # market carrying a `score` specifier, which is a different bet with a
    # different outcome block (`442`-…). Keyed on the id, not on the label they
    # nearly share.
    _m(
        "81",
        "correct_score",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Correct Score",
        "1st Half - Correct Score; 1st half - correct score",
    ),
    _m("83", "1x2", PERIOD_2H, RULE_SECOND_HALF, "2nd Half - 1X2", "2nd Half - 1X2; 2nd Half 1X2"),
    _m(
        "84",
        "next_goal",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Next Goal",
        "2nd Half - 1st Goal; 2nd half - 1st goal",
    ),
    _m(
        "85",
        "double_chance",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Double Chance",
        "2nd Half - Double Chance",
    ),
    _m(
        "86",
        "draw_no_bet",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Draw No Bet",
        "2nd Half - Draw No Bet; 2nd half - DNB",
    ),
    _m(
        "87",
        "handicap",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Handicap (1X2)",
        "2nd Half - Handicap (1x2); 2nd Half - Handicap 0:1; 2nd half - Handicap",
    ),
    _m(
        "88",
        "asian_handicap",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Asian Handicap",
        "2nd Half - Asian Handicap; 2nd half - Asian Handicap",
    ),
    _m(
        "90",
        "total_goals",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Over/Under",
        "2nd Half - Over/Under; 2nd half - O/U; 2nd half - Total",
    ),
    _m(
        "91",
        "total_goals_home",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Home Total",
        "2nd Half - Home Total; 2nd half - Home O/U",
    ),
    _m(
        "92",
        "total_goals_away",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Away Total",
        "2nd Half - Away Total; 2nd half - Away O/U",
    ),
    _m(
        "93",
        "exact_goals",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Exact Goals",
        "2nd Half - Exact Goals; 2nd half - Exact goals",
    ),
    _m(
        "94",
        "odd_even_goals",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Odd/Even",
        "2nd Half - Odd/Even; 2nd half - Odd/Even",
    ),
    _m(
        "95",
        "btts",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Both Teams To Score",
        "2nd Half - GG/NG; 2nd Half GG|NG; 2nd half - Both teams to score",
    ),
    _m(
        "96",
        "clean_sheet_home",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Home Clean Sheet",
        "2nd Half - Home Team Clean Sheet; 2nd half - Home clean sheet",
    ),
    _m(
        "97",
        "clean_sheet_away",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Away Clean Sheet",
        "2nd Half - Away Team Clean Sheet; 2nd half -Away clean sheet",
    ),
    _m(
        "98",
        "correct_score",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Correct Score",
        "2nd Half - Correct Score; 2nd half -Correct score",
    ),
    # --- minute-interval markets: D26(b)'s `other` --------------------------
    # "when will the goal be scored" settles on a minute bucket. No score basis
    # answers it, so forcing one would be silently wrong on every fixture.
    #
    # All five minute-window markets **pin their name** (0005b verifier, Obs 3).
    # Their observed labels are either a betradar template ("{!goalnr}",
    # "{from} to {to}") or carry the window this run happened to sample most, so
    # a most-frequent-label name reads as though the market were one specific
    # window — "1X2 from 1 to 85 minute" one run and "1X2 from 1 to 15 minute"
    # the next, on an unchanged identity. The window is a specifier, so it is a
    # line, so it lives on `dim_outcome.side_or_line`; the market's name must
    # say what the market is, not which window was in the sample.
    #
    # `100` and `101` stay two families: the 15- and 10-minute widths are fixed
    # by the betradar id rather than sampled, and their outcome id blocks are
    # disjoint (`584`-`596` against `598`-`616`). Their names name a market
    # property, which is not the defect Obs 3 describes.
    _m(
        "100",
        "goal_minute_interval_15",
        PERIOD_MATCH,
        RULE_OUTSIDE_THE_FOUR,
        "Goal Minute Interval (15 min)",
        "When will the {!goalnr} goal be scored (15 min interval)",
        pin_name=True,
    ),
    _m(
        "101",
        "goal_minute_interval_10",
        PERIOD_MATCH,
        RULE_OUTSIDE_THE_FOUR,
        "Goal Minute Interval (10 min)",
        "When will the xth goal be scored (10 min interval)",
        pin_name=True,
    ),
    _m(
        "105",
        "1x2_ten_minute_interval",
        PERIOD_MATCH,
        RULE_OUTSIDE_THE_FOUR,
        "1X2 (10 Minute Interval)",
        "10 minutes-1x2 from {from} to {to}",
        pin_name=True,
    ),
    # --- goal bounds / excluded goals ---------------------------------------
    _m(
        "450001",
        "goal_bounds",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Goal Bounds",
        "Goal Bounds",
    ),
    _m(
        "450002",
        "goal_bounds_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Goal Bounds - Home",
        "Goal Bounds - Home",
    ),
    _m(
        "450003",
        "goal_bounds_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Goal Bounds - Away",
        "Goal Bounds - Away",
    ),
    _m(
        "450004",
        "excluded_goals",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Excluded Number of Goals",
        "Excluded Number of Goals",
    ),
    _m(
        "450005",
        "excluded_goals_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Excluded Number of Goals - Home",
        "Excluded Number of Goals - Home",
    ),
    _m(
        "450006",
        "excluded_goals_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Excluded Number of Goals - Away",
        "Excluded Number of Goals - Away",
    ),
    # sportybet's private half-twins of `450001`/`450004` (task 0005e; 0005d
    # Obs 1 named the hole: these are half twins of match-level families that
    # were left uncompleted). **Not** `(goal_bounds, 1h)` / `(excluded_goals,
    # 1h)`: betradar reuses a narrower, capped bucket scheme for the half
    # (`13` is "1-3+" here against "1-3" at match level, `33` is "3+" against
    # plain "3" — the same id means a different bucket), exactly the collision
    # `SIDES_GOAL_BOUNDS_TEAM`/`SIDES_EXCLUDED_GOALS_TEAM` already exist to hold
    # for the home/away team totals. One flat `sides` dict per family cannot
    # hold two different values for one id, so this gets its own family rather
    # than silently mislabelling one of the two periods (D16) — the same
    # reasoning `819` uses to stay off `818`'s family (D26(c) note above), here
    # forced by the outcome vocabulary rather than the time basis. Confirmed
    # live: `810001`'s nine outcome ids and their `desc` text are byte-identical
    # to `SIDES_GOAL_BOUNDS_TEAM`'s existing values; `810002`'s four to
    # `SIDES_EXCLUDED_GOALS_TEAM`'s. No 2nd-half id is observed for either in
    # this cohort — genuinely absent, not a hole left open.
    _m(
        "810001",
        "goal_bounds_first_half",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "Goal Bounds - 1st Half",
        "Goal Bounds - 1st Half",
    ),
    _m(
        "810002",
        "excluded_goals_first_half",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "Excluded Number of Goals - First Half",
        "Excluded Number of Goals - First Half",
    ),
    # --- combination markets ------------------------------------------------
    _m(
        "546",
        "double_chance_and_btts",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Double Chance & Both Teams To Score",
        "Double Chance & GG/NG; Double chance & both teams to score",
    ),
    _m(
        "547",
        "double_chance_and_total_goals",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Double Chance & Total",
        "Double Chance & Over/Under 4.5; Double Chance & Total; Double chance & O/U",
    ),
    # `540`/`541` ("Double Chance & 1st/2nd Half GG/NG") cross a match-level
    # Double Chance leg with a *half*-scoped BTTS leg — structurally the same
    # shape as `819` crossing HT/FT with a half leg, and decided the same way
    # (D26(c)): rule 1/2 ("wholly on a half") does not apply because the DC leg
    # needs the whole match, so `regular` is right, and each gets its own
    # family rather than being folded into `546`'s (`double_chance_and_btts`)
    # 1h/2h rows — those (`542`/`545` below) are markets whose *both* legs are
    # scoped to the half, a different bet. Naming `540`/`541` as a period of
    # `546` would put two different bets on one `dim_market` row.
    _m(
        "540",
        "double_chance_and_first_half_btts",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Double Chance & 1st Half Both Teams To Score",
        "Double Chance & 1st Half GG/NG; Double chance  & 1st half both teams score;"
        " Double Chance (match) & 1st Half Both Teams Score",
    ),
    _m(
        "541",
        "double_chance_and_second_half_btts",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Double Chance & 2nd Half Both Teams To Score",
        "Double Chance & 2nd Half GG/NG; Double chance (match) & 2nd half both teams"
        " score; Double Chance (match) & 2nd Half Both Teams Score",
    ),
    # 542-545/552-553: the half counterparts of `546` / `35` / `37` / `548`,
    # where *both* legs settle wholly on the half.
    _m(
        "542",
        "double_chance_and_btts",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Double Chance & Both Teams To Score",
        "1st Half - Double Chance & Both Teams To Score; 1st Half - Double Chance & GG/NG",
    ),
    _m(
        "543",
        "1x2_and_btts",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - 1X2 & Both Teams To Score",
        "2nd Half - 1X2 & GG/NG; 2nd Half - 1x2 & Both Teams To Score",
    ),
    _m(
        "544",
        "1x2_and_total_goals",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - 1X2 & Total",
        "2nd Half - 1X2 & Over/Under 1.5; 2nd Half - 1x2 & Total",
    ),
    _m(
        "545",
        "double_chance_and_btts",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Double Chance & Both Teams To Score",
        "2nd Half -  Double Chance & GG/NG; 2nd Half - Double Chance & Both Teams To Score",
    ),
    _m(
        "548",
        "multigoals",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Multigoals",
        "Multigoals",
    ),
    _m(
        "549",
        "multigoals_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home Multigoals",
        "Home Multigoals",
    ),
    # `550` "Away Multigoals" (task 0007b) — mirrors `549` at the same grain,
    # same 5-outcome block (`1746`-`1749`+`1805`). Its own family, not a
    # period of `multigoals_home`: the `home`/`away` scope is a family axis
    # (D5), the same way `19`/`20` team totals are two families rather than
    # a "team" specifier under one.
    _m(
        "550",
        "multigoals_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away Multigoals",
        "Away Multigoals; Away multigoals",
    ),
    # `551` "Multiscores" (task 0007b) — a distinct family from `548`
    # `multigoals`: a smaller vocabulary of specific score buckets ("1:0 or
    # 2:0 or 3:0", etc.) that is a different bet from counting the total
    # number of goals. Observed in all three books with the same id block
    # (`1750`-`1759`+`1803`). Regular time.
    _m(
        "551",
        "multiscores",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Multiscores",
        "Multiscores",
    ),
    # `552`/`553` publish the 5-outcome block `1746`-`1749`+`1805` that `549`
    # uses at match level — a half has fewer goals, so betradar reuses the
    # narrower bucket set. They are `multigoals` at a half, **not**
    # `multigoals_home`: the shared outcome block is a coincidence of size, and
    # the label says which.
    _m(
        "552",
        "multigoals",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Multigoals",
        "1st Half - Multigoals; 1st half - Multigoals",
    ),
    _m(
        "553",
        "multigoals",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Multigoals",
        "2nd Half - Multigoals; 2nd half - Multigoals",
    ),
    # --- goal-streak and early-payout markets -------------------------------
    _m(
        "60020",
        "goal_streak_any",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Any Team To Score 3 or More Goals in a Row",
        "Any Team To Score 3 or More Goals in a Row",
    ),
    _m(
        "60021",
        "goal_streak_home",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home Team To Score 3 or More Goals in a Row",
        "Home Team To Score 3 or More Goals in a Row",
    ),
    _m(
        "60022",
        "goal_streak_away",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away Team To Score 3 or More Goals in a Row",
        "Away Team To Score 3 or More Goals in a Row",
    ),
    # 60180: "if N goals are scored by the Mth minute your Over is settled as
    # won" — a minute condition, and the market's own rules text says the final
    # result no longer matters once it triggers. Not one of the four (D26(b)).
    _m(
        "60180",
        "total_goals_early_goals",
        PERIOD_MATCH,
        RULE_OUTSIDE_THE_FOUR,
        "Over/Under - Early Goals",
        "Over/Under - Early Goals",
    ),
    # 60200 (1UP): settled early on a one-goal lead, "once settled, the final
    # result no longer matters" — the recorded sportybet payload's own
    # marketGuide. Same class as 60180.
    _m(
        "60200",
        "1x2_one_up",
        PERIOD_MATCH,
        RULE_OUTSIDE_THE_FOUR,
        "1X2 - 1UP",
        "1X2 - 1UP; 1x2 - 1UP",
    ),
    # --- halftime/fulltime combinations, to-win markets, more props ----------
    _m(
        "818",
        "halftime_fulltime_and_total_goals",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Halftime/Fulltime & Total",
        "Halftime/Fulltime & Over/Under 1.5; Halftime/fulltime & total",
    ),
    # 819: carries "1st Half" wording for one leg only — see the application
    # note. Its HT/FT leg needs the regular-time result, so `1h` would
    # mis-settle it; the family is distinct from 818's so the two ids cannot
    # collide on `(family, period)`.
    _m(
        "819",
        "halftime_fulltime_and_first_half_total_goals",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Halftime/Fulltime & 1st Half Total",
        "Halftime/Fulltime & 1st Half Over/Under 0.5; Halftime/fulltime & 1st half total",
    ),
    # `820` "Halftime/Fulltime & Exact Goals" (task 0007b) — its own family;
    # crosses the HT/FT result with an exact match goals bucket. Regular time
    # per the D26(c) reading `47`/`818`/`819` already use: both legs settle on
    # the regular-time result, and D18 stores h1 alongside reg so the
    # calculator has every score it needs. `time_basis = other` would drop a
    # market D20 puts inside v1 settlement scope.
    _m(
        "820",
        "halftime_fulltime_and_exact_goals",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Halftime/Fulltime & Exact Goals",
        "Halftime/Fulltime & Exact Goals; Halftime/fulltime & exact goals",
    ),
    _m(
        "879",
        "away_to_win",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Away To Win",
        "Away To Win",
    ),
    _m(
        "880",
        "home_to_win",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Home To Win",
        "Home To Win",
    ),
    _m(
        "881",
        "any_team_to_win",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Any Team To Win",
        "Any Team To Win",
    ),
    _m(
        "898",
        "player_to_score_two_plus",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Player to Score 2+",
        "Player to Score 2+; Player to score 2+",
        prop=True,
    ),
    _m(
        "899",
        "player_to_score_three_plus",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Player to Score 3+",
        "Player to Score 3+; Player to score 3+",
        prop=True,
    ),
    # `800097`/`800109` (task 0007b) — sportybet player-prop markets whose
    # outcomes are all `pre:playerprops:<event>:<player>[:N[:team]]` structured
    # ids. They resolve identity-first, exactly the shape D13 defers
    # settlement for: `is_player_prop = True`, `player_key` stays NULL, the
    # raw id in `side_or_line` keeps every player distinct under
    # `NULLS NOT DISTINCT`. See `betradar_outcome_map.py`'s prop notes.
    _m(
        "800097",
        "player_fouls_won",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Player Fouls Won",
        "Player Fouls Won",
        prop=True,
    ),
    _m(
        "800109",
        "player_not_to_score",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "Player Not to Score",
        "Player Not to Score",
        prop=True,
    ),
    # --- sportybet's minute-range markets: the reason D26(b) exists ----------
    _m(
        "900041",
        "no_draw_and_btts",
        PERIOD_MATCH,
        RULE_NO_PERIOD_SIGNAL,
        "No Draw & Both Teams To Score",
        "No Draw Both Teams To Score Yes/No",
    ),
    # sportybet's private half-twins of `33` (`win_to_nil_home`) and `547`
    # (`double_chance_and_total_goals`) — task 0005e, the other half of 0005d
    # Obs 1's hole. Both settle wholly on their half (the market's own
    # `marketGuide` text names only that half for every leg), so rule 1/2
    # applies plainly — unlike `540`/`541` above, there is no match-scoped leg
    # here to trigger D26(c). Outcome ids are sportybet-private (`39`/`40` for
    # the win-to-nil pair, `145`-`150` for the double-chance-and-total pair) and
    # do not collide with the match-level family's existing ids, so both join
    # their match-level family's `sides` table rather than needing one of
    # their own (`betradar_outcome_map.py`).
    _m(
        "900036",
        "win_to_nil_home",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half Home Team to Win to Nil",
        "1st Half Home Team to Win to Nil",
    ),
    _m(
        "900038",
        "win_to_nil_home",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half Home Team to Win to Nil",
        "2nd Half Home Team to Win to Nil",
    ),
    _m(
        "900042",
        "double_chance_and_total_goals",
        PERIOD_1H,
        RULE_FIRST_HALF,
        "1st Half - Double Chance & Total",
        "Half-time Double Chance & Total Goals",
    ),
    _m(
        "900043",
        "double_chance_and_total_goals",
        PERIOD_2H,
        RULE_SECOND_HALF,
        "2nd Half - Double Chance & Total",
        "2nd Half Double Chance & Total Goals",
    ),
    _m(
        "900069",
        "1x2_minute_range",
        PERIOD_MATCH,
        RULE_OUTSIDE_THE_FOUR,
        "1X2 (Minute Range)",
        "1X2 from 1 to 85 minute; 1X2 from 1 to 15 minute",
        pin_name=True,
    ),
    # The cohort's largest single market by volume. `regular` here would
    # mis-settle it silently on every fixture where a goal falls outside the
    # window — D26(b)'s worked example.
    _m(
        "900313",
        "total_goals_minute_range",
        PERIOD_MATCH,
        RULE_OUTSIDE_THE_FOUR,
        "Total Goals Over/Under (Minute Range)",
        "Total Goals Over/Under from 1 to 85 minute",
        pin_name=True,
    ),
)

BETRADAR_MARKET_MAP: Final[dict[str, MarketMapping]] = {m.market_id: m for m in _MAPPINGS}


def lookup(market_id: str) -> MarketMapping | None:
    """The mapping for a bare betradar market id, or None if it is unmapped.

    None is the honest answer and the caller must treat it as one: count it,
    log it, write no row. It is never an `other`.
    """
    return BETRADAR_MARKET_MAP.get(market_id)
