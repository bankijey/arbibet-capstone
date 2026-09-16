"""The betradar outcome decision table: family → line key(s) + side vocabulary.

**This module is data, and it is meant to be read.** It is the sibling of
`betradar_market_map.py` one grain down: that table says what a *market* id
means, this one says what an *outcome* id means inside it, and which specifier
key carries the line.

Evidence: every side below was read off live `bronze_event_payloads` bodies for
all three books in D22's cohort (task 0005d), not from betradar documentation
and not from any book's label. Where the three books publish an id, they publish
**the same id** — the labels differ ("1/2" to ilotbet, "1 2" to msport, "Home or
Away" to sportybet, all under outcome `10`) and the ids do not. That is the
whole reason identity is keyed on ids here.

## Resolve on ids, never on labels

`docs/betradar-taxonomy-survey.md` states it and proves it: sportybet
substitutes the team name into market `19` ("B36 Torshavn Over/Under") where
ilotbet says "Home Total", and into `818`/`819`'s outcome labels
("WKW ETO FC Gyor/draw & under 4.5" for outcome `1837`, which msport and
sportybet both label "Home/Draw & Under 2.5"). A label may be **stored** on
`dim_outcome.label`; it may never determine identity. Nothing in this module
reads a label.

## `side_or_line` — the canonical format

One format, stated once, read by the whole platform:

    side_or_line := <side>                  when the family declares no line key
                  | <side> "@" <line>       when it declares one or more

    <line>       := the values of the family's declared specifier keys, in the
                    declared order, joined by ";"

`<side>` is lowercase, carries no book text, and is one of two things:

1. A **canonical token** from `sides` below, for betradar's bare numeric outcome
   ids — whose meaning is family-dependent (`12` is "over" in a total and means
   nothing in a 1X2), which is why the table is keyed by family.
2. A **betradar structured outcome id, verbatim** (lowercased) — `sr:player:…`,
   `sr:exact_goals:3+:88`, `sr:goal_range:7+:1342`, `sr:winning_margin:3+:113`.
   These are betradar's own, self-describing (the variant is inside the id), and
   identical across books, so they *are* already a canonical side. Inventing a
   prettier token for them would only add a place to be wrong.

`<line>` values are canonicalised so that two books writing the same line the
same way land on one row: trimmed, lowercased, a decimal comma becomes a dot,
a leading `+` is dropped, and a numeric value is reduced to its shortest exact
decimal form (`2.50` → `2.5`, `1.0` → `1`, `+2.5` → `2.5`, `-0.50` → `-0.5`).
Non-numeric values pass through lowercased, so a handicap stays `0:1` and a
minute stays `85`.

Worked examples, all from real payloads:

    1x2                        home              draw              away
    total_goals                over@2.5          under@2.5
    handicap                   home@0:1          draw@0:1          away@0:1
    asian_handicap             home@-0.5         away@-0.5
    correct_score              0:0               1:0               other
    exact_goals                sr:exact_goals:3+:88
    goal_range                 sr:goal_range:7+:1342      (sportybet, msport)
                               sr:point_range:6+:1121     (ilotbet — disjoint)
    goalscorer_nth             sr:player:1004865@1        no_goal@1
    goalscorer_anytime         sr:player:1004865          no_goal
    total_goals_minute_range   over@85;2.5
    1x2_ten_minute_interval    home@1;10

## The player-prop trap this format exists to defuse (D13 + D25(b))

`dim_outcome`'s natural key is
`UNIQUE NULLS NOT DISTINCT (market_key, side_or_line, player_key)`. D13 defers
the player crosswalk, so **every** player-prop outcome silver mints carries
`player_key = NULL` — and under `NULLS NOT DISTINCT` two NULLs are equal. If
`side_or_line` did not distinguish the players, every player in a goalscorer
market would collapse into **one row**, with no error and no warning: the first
player's row would silently absorb every other player's odds.

Carrying the raw `sr:player:` id in `side_or_line` is what keeps them distinct.
The consequence has to be written down now rather than rediscovered later:

    Filling `player_key` mutates the natural key. The D13 crosswalk task must
    DELETE-then-INSERT these rows (and re-point `outcome_xref`), because an
    upsert keyed on the new `(market_key, side_or_line, player_key)` will not
    match the existing NULL-keyed row — it will insert a second one and leave
    the crosswalk pointing at the first. That is D25(b) exactly, one grain down.

`1716` is **not** a player. It is the "no goalscorer" outcome and every book in
the cohort publishes it under `38`/`39`/`40` (labelled "No Goal", "no goal").
It resolves through the ordinary numeric side table, as `no_goal`.

## Market `25` — one id, two vocabularies, and no winner picked

sportybet and msport publish `sr:goal_range:7+:1342…1345` under market `25`;
ilotbet publishes `sr:point_range:6+:1121…1124`. Different vocabulary **and**
different thresholds (7+ against 6+), so they are not the same outcome set and
must not be unified on the strength of a shared market id. Because the side is
the structured id verbatim, they resolve to disjoint `dim_outcome` rows under
one `dim_market` row without anything special happening — which is the honest
result. The resolver logs it as a known divergence
(`betradar_outcomes.disjoint_outcome_sets`) and discards neither vocabulary.

## Which specifier key is the line

It varies by family — `total` for over/unders, `hcp` for handicaps, `goalnr` for
nth-goal markets, `minute` or `from`/`to` for minute windows — so it belongs
beside the family, here, and not in a branch inside the parse. A family the
table does not cover, or one whose declared line key the payload does not carry,
resolves to **unclassified**: counted, logged, no row written. That is 0005b's
rule at this grain, and it is why `type=prematch|live` is deliberately *not* a
line — it names which feed produced the listing, not what was bet on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final

from arbibet_capstone.crosswalk.settle.payload_shapes import PLAYER_ID_PREFIXES

#: Separates the side from the line. Chosen because no betradar outcome id,
#: canonical token or specifier value observed in the cohort contains it.
LINE_SEPARATOR: Final = "@"
#: Joins the values of a multi-key line, in the family's declared key order.
LINE_VALUE_SEPARATOR: Final = ";"

#: Specifier keys that qualify the *listing* rather than the bet. `type` is
#: `prematch`/`live` — the feed a market came from. Never part of an identity.
NON_LINE_SPECIFIER_KEYS: Final = frozenset({"type", "version"})


# --- shared side vocabularies ------------------------------------------------
#
# Betradar reuses one outcome-id block across many markets, so these are named
# once and referenced by every family that publishes them. A block shared by two
# families is not a merge: the families are different `dim_market` rows, so
# `1746 → "1-2"` under `multigoals` and under `multigoals_home` are two distinct
# canonical outcomes that happen to spell their side the same way.

SIDES_1X2: Final[dict[str, str]] = {"1": "home", "2": "draw", "3": "away"}
SIDES_OVER_UNDER: Final[dict[str, str]] = {"12": "over", "13": "under"}
SIDES_YES_NO: Final[dict[str, str]] = {"74": "yes", "76": "no"}
SIDES_DOUBLE_CHANCE: Final[dict[str, str]] = {
    "9": "home_or_draw",
    "10": "home_or_away",
    "11": "draw_or_away",
}
SIDES_NO_BET_HOME_AWAY: Final[dict[str, str]] = {"4": "home", "5": "away"}
SIDES_TEAM_OR_NONE: Final[dict[str, str]] = {"6": "home", "7": "none", "8": "away"}
SIDES_HANDICAP: Final[dict[str, str]] = {"1711": "home", "1712": "draw", "1713": "away"}
SIDES_ASIAN_HANDICAP: Final[dict[str, str]] = {"1714": "home", "1715": "away"}
SIDES_ODD_EVEN: Final[dict[str, str]] = {"70": "odd", "72": "even"}
SIDES_HIGHEST_SCORING_HALF: Final[dict[str, str]] = {"436": "1h", "438": "2h", "440": "equal"}
SIDES_1X2_AND_BTTS: Final[dict[str, str]] = {
    "78": "home_and_yes",
    "80": "home_and_no",
    "82": "draw_and_yes",
    "84": "draw_and_no",
    "86": "away_and_yes",
    "88": "away_and_no",
}
SIDES_TOTAL_AND_BTTS: Final[dict[str, str]] = {
    "90": "over_and_yes",
    "92": "under_and_yes",
    "94": "over_and_no",
    "96": "under_and_no",
}
SIDES_1X2_AND_TOTAL: Final[dict[str, str]] = {
    "794": "home_and_under",
    "796": "home_and_over",
    "798": "draw_and_under",
    "800": "draw_and_over",
    "802": "away_and_under",
    "804": "away_and_over",
}
SIDES_DOUBLE_CHANCE_AND_BTTS: Final[dict[str, str]] = {
    "1718": "home_or_draw_and_yes",
    "1719": "home_or_draw_and_no",
    "1720": "home_or_away_and_yes",
    "1721": "home_or_away_and_no",
    "1722": "draw_or_away_and_yes",
    "1723": "draw_or_away_and_no",
}
SIDES_DOUBLE_CHANCE_AND_TOTAL: Final[dict[str, str]] = {
    "1724": "home_or_draw_and_under",
    "1725": "home_or_away_and_under",
    "1726": "draw_or_away_and_under",
    "1727": "home_or_draw_and_over",
    "1728": "home_or_away_and_over",
    "1729": "draw_or_away_and_over",
}
SIDES_TEAMS_TO_SCORE: Final[dict[str, str]] = {
    "784": "none",
    "788": "only_home",
    "790": "only_away",
    "792": "both",
}
#: The score itself is the canonical side — book-neutral and self-describing,
#: so the calculator compares it against D18's period-resolved score directly.
#: Three disjoint id blocks: match (`45`), 1st half (`81`), 2nd half (`98`).
SIDES_CORRECT_SCORE: Final[dict[str, str]] = {
    # match — `45`
    "274": "0:0", "276": "1:0", "278": "2:0", "280": "3:0", "282": "4:0",
    "284": "0:1", "286": "1:1", "288": "2:1", "290": "3:1", "292": "4:1",
    "294": "0:2", "296": "1:2", "298": "2:2", "300": "3:2", "302": "4:2",
    "304": "0:3", "306": "1:3", "308": "2:3", "310": "3:3", "312": "4:3",
    "314": "0:4", "316": "1:4", "318": "2:4", "320": "3:4", "322": "4:4",
    "324": "other",
    # 1st half — `81`
    "462": "0:0", "464": "1:1", "466": "2:2", "468": "1:0", "470": "2:0",
    "472": "2:1", "474": "0:1", "476": "0:2", "478": "1:2", "480": "other",
    # 2nd half — `98`
    "546": "0:0", "548": "0:1", "550": "0:2", "552": "1:0", "554": "1:1",
    "556": "1:2", "558": "2:0", "560": "2:1", "562": "2:2", "564": "other",
}  # fmt: skip
SIDES_HALFTIME_FULLTIME: Final[dict[str, str]] = {
    "418": "home_home", "420": "home_draw", "422": "home_away",
    "424": "draw_home", "426": "draw_draw", "428": "draw_away",
    "430": "away_home", "432": "away_draw", "434": "away_away",
}  # fmt: skip
#: `1836`–`1853`: the HT/FT leg crossed with under (`1836`–`1844`) then over
#: (`1845`–`1853`). Shared verbatim by `818` and `819`, which are different
#: families — so the same side token under two `dim_market` rows, as intended.
SIDES_HALFTIME_FULLTIME_AND_TOTAL: Final[dict[str, str]] = {
    "1836": "home_home_and_under", "1837": "home_draw_and_under",
    "1838": "home_away_and_under", "1839": "draw_home_and_under",
    "1840": "draw_draw_and_under", "1841": "draw_away_and_under",
    "1842": "away_home_and_under", "1843": "away_draw_and_under",
    "1844": "away_away_and_under",
    "1845": "home_home_and_over", "1846": "home_draw_and_over",
    "1847": "home_away_and_over", "1848": "draw_home_and_over",
    "1849": "draw_draw_and_over", "1850": "draw_away_and_over",
    "1851": "away_home_and_over", "1852": "away_draw_and_over",
    "1853": "away_away_and_over",
}  # fmt: skip
#: The bucket ranges are the sides. `548` (match) and `552`/`553` (halves) use
#: two disjoint blocks; both live here because the family is one.
SIDES_MULTIGOALS: Final[dict[str, str]] = {
    "1730": "1-2", "1731": "1-3", "1732": "1-4", "1733": "1-5", "1734": "1-6",
    "1735": "2-3", "1736": "2-4", "1737": "2-5", "1738": "2-6", "1739": "3-4",
    "1740": "3-5", "1741": "3-6", "1742": "4-5", "1743": "4-6", "1744": "5-6",
    "1745": "7+", "1804": "no_goal",
    "1746": "1-2", "1747": "1-3", "1748": "2-3", "1749": "4+", "1805": "no_goal",
}  # fmt: skip
SIDES_MULTIGOALS_HOME: Final[dict[str, str]] = {
    "1746": "1-2", "1747": "1-3", "1748": "2-3", "1749": "4+", "1805": "no_goal",
}  # fmt: skip
#: `1716` is the "no goalscorer" outcome, published by all three books under
#: `38`/`39`/`40`. **It is not a player** and must not take the player path.
SIDES_GOALSCORER: Final[dict[str, str]] = {"1716": "no_goal"}
SIDES_GOAL_MINUTE_INTERVAL_15: Final[dict[str, str]] = {
    "584": "1-15", "586": "16-30", "588": "31-45",
    "590": "46-60", "592": "61-75", "594": "76-90", "596": "none",
}  # fmt: skip
SIDES_GOAL_MINUTE_INTERVAL_10: Final[dict[str, str]] = {
    "598": "1-10", "600": "11-20", "602": "21-30", "604": "31-40", "606": "41-50",
    "608": "51-60", "610": "61-70", "612": "71-80", "614": "81-90", "616": "none",
}  # fmt: skip
#: sportybet's `450001` block spells the bounds into the id (`12` → "1-2"), and
#: its `900313` uses `30`/`31` for over/under rather than betradar's `12`/`13`.
#: Read off the payload, not assumed from the betradar block.
SIDES_GOAL_BOUNDS: Final[dict[str, str]] = {
    "0": "0", "1": "0-1", "2": "0-2", "3": "0-3", "4": "0-4",
    "11": "1", "12": "1-2", "13": "1-3", "14": "1-4", "15": "1-5+",
    "22": "2", "23": "2-3", "24": "2-4", "25": "2-5+",
    "33": "3", "34": "3-4", "35": "3-5+", "44": "4", "45": "4-5+", "55": "5+",
}  # fmt: skip
SIDES_GOAL_BOUNDS_TEAM: Final[dict[str, str]] = {
    "0": "0", "1": "0-1", "2": "0-2", "11": "1", "12": "1-2",
    "13": "1-3+", "22": "2", "23": "2-3+", "33": "3+",
}  # fmt: skip
SIDES_EXCLUDED_GOALS: Final[dict[str, str]] = {
    "0": "0", "1": "1", "2": "2", "3": "3", "4": "4", "5": "5+",
}  # fmt: skip
SIDES_EXCLUDED_GOALS_TEAM: Final[dict[str, str]] = {
    "0": "0", "1": "1", "2": "2", "3": "3+",
}  # fmt: skip
SIDES_OVER_UNDER_SPORTYBET_MINUTE: Final[dict[str, str]] = {"30": "over", "31": "under"}
SIDES_YES_NO_SPORTYBET: Final[dict[str, str]] = {"39": "yes", "40": "no"}
#: sportybet's private half-twins of `547` (`900042`/`900043`) use their own
#: id block rather than `547`'s `1724`-`1729` — disjoint, so both live in the
#: one family's `sides` (task 0005e).
SIDES_DOUBLE_CHANCE_AND_TOTAL_SPORTYBET_HALF: Final[dict[str, str]] = {
    "145": "home_or_draw_and_over", "146": "home_or_away_and_over",
    "147": "draw_or_away_and_over", "148": "home_or_draw_and_under",
    "149": "home_or_away_and_under", "150": "draw_or_away_and_under",
}  # fmt: skip
#: `55` "1st/2nd Half Both Teams To Score" — a single bet on BTTS status per half,
#: outcomes are (1h_btts, 2h_btts) pairs. Ids observed live in all three books
#: (task 0007b). Same shape a HT/FT market has, but on BTTS rather than 1X2.
SIDES_FIRST_AND_SECOND_HALF_BTTS: Final[dict[str, str]] = {
    "806": "no_no", "808": "yes_no", "810": "yes_yes", "812": "no_yes",
}  # fmt: skip
#: `184` "Xth Goal & 1X2" — the goal identity (home/away) crossed with the
#: match 1X2. `826` is "No goal" (no Xth goal scored at all). Ids observed live
#: in all three books; the `goalnr` specifier picks which goal (task 0007b).
SIDES_XTH_GOAL_AND_1X2: Final[dict[str, str]] = {
    "814": "home_goal_and_home", "816": "home_goal_and_draw", "818": "home_goal_and_away",
    "820": "away_goal_and_home", "822": "away_goal_and_draw", "824": "away_goal_and_away",
    "826": "no_goal",
}  # fmt: skip
#: `46` "Halftime/Fulltime Correct Score" — outcomes are `<ht_score> <ft_score>`
#: pairs, with `4+` collapsing outcomes at 4 goals or more per team. Betradar
#: emits only combinations that are actually reachable (no "Home/Home & 0", no
#: "Away/Home & 0"), so the id block is sparse — read verbatim off live payloads
#: in all three books (task 0007b), never enumerated by product.
SIDES_HALFTIME_FULLTIME_CORRECT_SCORE: Final[dict[str, str]] = {
    "326": "0:0_0:0", "328": "0:0_0:1", "330": "0:0_0:2", "332": "0:0_0:3",
    "334": "0:0_1:0", "336": "0:0_1:1", "338": "0:0_1:2", "340": "0:0_2:0",
    "342": "0:0_2:1", "344": "0:0_3:0", "346": "0:0_4+",
    "348": "0:1_0:1", "350": "0:1_0:2", "352": "0:1_0:3", "354": "0:1_1:1",
    "356": "0:1_1:2", "358": "0:1_2:1", "360": "0:1_4+",
    "362": "0:2_0:2", "364": "0:2_0:3", "366": "0:2_1:2", "368": "0:2_4+",
    "370": "0:3_0:3", "372": "0:3_4+",
    "374": "1:0_1:0", "376": "1:0_1:1", "378": "1:0_1:2", "380": "1:0_2:0",
    "382": "1:0_2:1", "384": "1:0_3:0", "386": "1:0_4+",
    "388": "1:1_1:1", "390": "1:1_1:2", "392": "1:1_2:1", "394": "1:1_4+",
    "396": "1:2_1:2", "398": "1:2_4+",
    "400": "2:0_2:0", "402": "2:0_2:1", "404": "2:0_3:0", "406": "2:0_4+",
    "408": "2:1_2:1", "410": "2:1_4+",
    "412": "3:0_3:0", "414": "3:0_4+",
    "416": "4+_4+",
}  # fmt: skip
#: `820` "Halftime/Fulltime & Exact Goals" — the HT/FT result crossed with an
#: exact goal count (`5+` at the cap). Read off live payloads in all three
#: books (task 0007b); `1854` is the sole "draw/draw & 0" outcome because a
#: 0-goal fixture can only end in that HT/FT combination.
SIDES_HALFTIME_FULLTIME_AND_EXACT_GOALS: Final[dict[str, str]] = {
    "1854": "draw_draw_and_0",
    "1855": "home_home_and_1", "1856": "draw_home_and_1", "1857": "draw_away_and_1",
    "1858": "away_away_and_1",
    "1859": "home_home_and_2", "1860": "home_draw_and_2", "1861": "draw_home_and_2",
    "1862": "draw_draw_and_2", "1863": "draw_away_and_2", "1864": "away_draw_and_2",
    "1865": "away_away_and_2",
    "1866": "home_home_and_3", "1867": "home_away_and_3", "1868": "draw_home_and_3",
    "1869": "draw_away_and_3", "1870": "away_home_and_3", "1871": "away_away_and_3",
    "1872": "home_home_and_4", "1873": "home_draw_and_4", "1874": "home_away_and_4",
    "1875": "draw_home_and_4", "1876": "draw_draw_and_4", "1877": "draw_away_and_4",
    "1878": "away_home_and_4", "1879": "away_draw_and_4", "1880": "away_away_and_4",
    "1881": "home_home_and_5+", "1882": "home_draw_and_5+", "1883": "home_away_and_5+",
    "1884": "draw_home_and_5+", "1885": "draw_draw_and_5+", "1886": "draw_away_and_5+",
    "1887": "away_home_and_5+", "1888": "away_draw_and_5+", "1889": "away_away_and_5+",
}  # fmt: skip
#: `551` "Multiscores" — a small vocabulary of score buckets. `1758`/`1759` are
#: sportybet-only "other homewin"/"other awaywin" catch-alls that betradar
#: itself publishes alongside the numbered buckets; kept verbatim as sides
#: because they are what the payload records — not a shrug (task 0007b).
SIDES_MULTISCORES: Final[dict[str, str]] = {
    "1750": "1:0_2:0_or_3:0",
    "1751": "0:1_0:2_or_0:3",
    "1752": "4:0_5:0_or_6:0",
    "1753": "0:4_0:5_or_0:6",
    "1754": "2:1_3:1_or_4:1",
    "1755": "1:2_1:3_or_1:4",
    "1756": "3:2_4:2_4:3_or_5:1",
    "1757": "2:3_2:4_3:4_or_1:5",
    "1758": "other_homewin",
    "1759": "other_awaywin",
    "1803": "draw",
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class FamilyOutcomes:
    """One `market_family`'s line key(s) and bare-numeric side vocabulary.

    A family with an empty `sides` resolves **only** structured betradar outcome
    ids (`exact_goals`, `goal_range`, `winning_margin`, the player props); a bare
    numeric id under it is unclassified rather than guessed at.
    """

    #: The specifier keys carrying the line, in the order they are joined into
    #: `side_or_line`. Empty when the family has no line — either it takes no
    #: specifier at all, or its `variant` is already inside every outcome id.
    line_keys: tuple[str, ...] = ()
    #: Bare numeric betradar outcome id → canonical side token.
    sides: dict[str, str] = field(default_factory=dict)
    #: Why this family's line is what it is, where a reader would otherwise ask.
    note: str = ""


def _f(
    line_keys: tuple[str, ...] = (),
    sides: dict[str, str] | None = None,
    note: str = "",
) -> FamilyOutcomes:
    return FamilyOutcomes(line_keys=line_keys, sides=sides or {}, note=note)


# ---------------------------------------------------------------------------
# The table. One row per `market_family` in `betradar_market_map.py`, grouped
# the way that table groups its ids. A family absent from here is unclassified
# at the outcome grain: its `dim_market` row still exists (its odds are real),
# but no `dim_outcome` row is minted for it and the miss is counted and logged.
# ---------------------------------------------------------------------------
FAMILY_OUTCOMES: Final[dict[str, FamilyOutcomes]] = {
    # --- match result ------------------------------------------------------
    "1x2": _f(sides=SIDES_1X2),
    "double_chance": _f(sides=SIDES_DOUBLE_CHANCE),
    "draw_no_bet": _f(sides=SIDES_NO_BET_HOME_AWAY),
    "home_no_bet": _f(sides={"776": "draw", "778": "away"}),
    "away_no_bet": _f(sides={"780": "home", "782": "draw"}),
    "next_goal": _f(
        line_keys=("goalnr",),
        sides=SIDES_TEAM_OR_NONE,
        note="`goalnr` is which goal — the line, not the market.",
    ),
    # `9` "Last Goal" (task 0007b) — sibling of `next_goal` (id `8`); same
    # ids/sides for home/none/away, no `goalnr` line because "last" is not a
    # sampled goal number, it is which team scored the *final* goal.
    "last_goal": _f(sides=SIDES_TEAM_OR_NONE),
    "teams_to_score": _f(sides=SIDES_TEAMS_TO_SCORE),
    "home_to_win": _f(sides=SIDES_YES_NO),
    "away_to_win": _f(sides=SIDES_YES_NO),
    "any_team_to_win": _f(sides=SIDES_YES_NO),
    # --- handicaps ---------------------------------------------------------
    "handicap": _f(
        line_keys=("hcp",),
        sides=SIDES_HANDICAP,
        note="`hcp` is a goal pair (`0:1`), not a number — it passes through.",
    ),
    "asian_handicap": _f(line_keys=("hcp",), sides=SIDES_ASIAN_HANDICAP),
    "winning_margin": _f(note="`variant` lives inside every `sr:winning_margin:` id."),
    # --- totals ------------------------------------------------------------
    "total_goals": _f(line_keys=("total",), sides=SIDES_OVER_UNDER),
    "total_goals_home": _f(line_keys=("total",), sides=SIDES_OVER_UNDER),
    "total_goals_away": _f(line_keys=("total",), sides=SIDES_OVER_UNDER),
    "both_halves_over": _f(line_keys=("total",), sides=SIDES_YES_NO),
    "both_halves_under": _f(line_keys=("total",), sides=SIDES_YES_NO),
    "odd_even_goals": _f(sides=SIDES_ODD_EVEN),
    # `27` and `28` (task 0007b) — Home/Away Team Odd/Even. Betradar reuses
    # `SIDES_ODD_EVEN` (`70`/`72`) across all three markets; each is its own
    # family because the *scope* differs (whole match vs one team's goals),
    # exactly the D5 rail (`19`/`20` team totals against `18` match total).
    "odd_even_home": _f(sides=SIDES_ODD_EVEN),
    "odd_even_away": _f(sides=SIDES_ODD_EVEN),
    # --- exact / range goals ----------------------------------------------
    # `variant` is inside every outcome id (`sr:exact_goals:3+:88`), so
    # declaring it as the line would spell it twice and make two books writing
    # the same selection differ if only one shipped the specifier.
    "exact_goals": _f(note="`variant` lives inside every `sr:exact_goals:` id."),
    "exact_goals_home": _f(note="`variant` lives inside every `sr:exact_goals:` id."),
    "exact_goals_away": _f(note="`variant` lives inside every `sr:exact_goals:` id."),
    "goal_range": _f(
        note=(
            "sportybet/msport publish `sr:goal_range:7+:*`, ilotbet"
            " `sr:point_range:6+:*` — a real divergence, kept disjoint."
        ),
    ),
    "multigoals": _f(sides=SIDES_MULTIGOALS),
    "multigoals_home": _f(sides=SIDES_MULTIGOALS_HOME),
    # `550` "Away Multigoals" (task 0007b) — mirrors `549` `multigoals_home`
    # with the same `SIDES_MULTIGOALS_HOME` block; the 5-outcome scheme
    # (`1746`-`1749`+`1805`) is what betradar uses for team-scoped multigoals
    # regardless of side. Confirmed live in all three books.
    "multigoals_away": _f(sides=SIDES_MULTIGOALS_HOME),
    # `551` "Multiscores" (task 0007b) — a small vocabulary of score buckets
    # published by all three books under the same betradar id block, with
    # `1758`/`1759`/`1803` filling the "other" cases (see the side dict above).
    "multiscores": _f(sides=SIDES_MULTISCORES),
    "goal_bounds": _f(sides=SIDES_GOAL_BOUNDS),
    "goal_bounds_home": _f(sides=SIDES_GOAL_BOUNDS_TEAM),
    "goal_bounds_away": _f(sides=SIDES_GOAL_BOUNDS_TEAM),
    # `810001`'s own family (task 0005e) — not `goal_bounds`'s 1h period. Its
    # nine outcome ids collide with `goal_bounds`'s (`13`/`23`/`33` mean a
    # different bucket at match level: "1-3"/"2-3"/"3" there, "1-3+"/"2-3+"/"3+"
    # here), so one flat `sides` dict cannot hold both without mislabelling one
    # of them (D16). The values are verbatim `SIDES_GOAL_BOUNDS_TEAM` — a half
    # caps at fewer goals, same as a team total, so betradar reused that bucket
    # scheme (confirmed live: every `desc` byte-matches it).
    "goal_bounds_first_half": _f(
        sides=SIDES_GOAL_BOUNDS_TEAM,
        note="Collides with `goal_bounds`'s ids at different values; own family (0005e).",
    ),
    "excluded_goals": _f(sides=SIDES_EXCLUDED_GOALS),
    "excluded_goals_home": _f(sides=SIDES_EXCLUDED_GOALS_TEAM),
    "excluded_goals_away": _f(sides=SIDES_EXCLUDED_GOALS_TEAM),
    # `810002`, same reasoning as `goal_bounds_first_half` above (task 0005e).
    "excluded_goals_first_half": _f(
        sides=SIDES_EXCLUDED_GOALS_TEAM,
        note="Collides with `excluded_goals`'s ids at different values; own family (0005e).",
    ),
    # --- both-teams-to-score and clean sheets ------------------------------
    "btts": _f(sides=SIDES_YES_NO),
    "clean_sheet_home": _f(sides=SIDES_YES_NO),
    "clean_sheet_away": _f(sides=SIDES_YES_NO),
    # `74`/`76` at match level (`33`); `39`/`40` on sportybet's private
    # half-twins (`900036`/`900038`, task 0005e) — disjoint ids, one family.
    "win_to_nil_home": _f(sides={**SIDES_YES_NO, **SIDES_YES_NO_SPORTYBET}),
    # `34` "Away Team to Win to Nil" (task 0007b) — mirrors `33` `win_to_nil_home`,
    # published by all three books with the same `SIDES_YES_NO` ids (`74`/`76`).
    # No sportybet half twin observed in this cohort, so no `SIDES_YES_NO_SPORTYBET`
    # merge is needed — parallels shipped only when the id was actually seen.
    "win_to_nil_away": _f(sides=SIDES_YES_NO),
    "goal_streak_any": _f(sides=SIDES_YES_NO),
    "goal_streak_home": _f(sides=SIDES_YES_NO),
    "goal_streak_away": _f(sides=SIDES_YES_NO),
    # --- score / half structure --------------------------------------------
    "correct_score": _f(sides=SIDES_CORRECT_SCORE),
    "halftime_fulltime": _f(sides=SIDES_HALFTIME_FULLTIME),
    # `46` "Halftime/Fulltime Correct Score" (task 0007b) — HT score paired
    # with FT score in one outcome id per (ht, ft) combination. Distinct from
    # `47` `halftime_fulltime` (which drops the numeric scores and keeps only
    # the results) and from `45` `correct_score` (final only), so it earns its
    # own family. Regular time — both legs read h1/reg from D18's period-
    # resolved scores, same reasoning as `47` and `819`.
    "halftime_fulltime_correct_score": _f(sides=SIDES_HALFTIME_FULLTIME_CORRECT_SCORE),
    "highest_scoring_half": _f(sides=SIDES_HIGHEST_SCORING_HALF),
    "highest_scoring_half_home": _f(sides=SIDES_HIGHEST_SCORING_HALF),
    "highest_scoring_half_away": _f(sides=SIDES_HIGHEST_SCORING_HALF),
    "win_both_halves_home": _f(sides=SIDES_YES_NO),
    "win_both_halves_away": _f(sides=SIDES_YES_NO),
    "win_either_half_home": _f(sides=SIDES_YES_NO),
    "win_either_half_away": _f(sides=SIDES_YES_NO),
    "score_in_both_halves_home": _f(sides=SIDES_YES_NO),
    "score_in_both_halves_away": _f(sides=SIDES_YES_NO),
    # --- combinations ------------------------------------------------------
    "1x2_and_btts": _f(sides=SIDES_1X2_AND_BTTS),
    "1x2_and_total_goals": _f(line_keys=("total",), sides=SIDES_1X2_AND_TOTAL),
    "total_goals_and_btts": _f(line_keys=("total",), sides=SIDES_TOTAL_AND_BTTS),
    "double_chance_and_btts": _f(sides=SIDES_DOUBLE_CHANCE_AND_BTTS),
    # `540`/`541`'s own families (task 0005e) — not `double_chance_and_btts`'s
    # 1h/2h rows, which are markets whose *both* legs are half-scoped. `540`/
    # `541` cross a match-level DC leg with a half-scoped BTTS leg (D26(c)), so
    # they settle `regular` and need their own identity; the outcome ids
    # (`1718`-`1723`) are the same block `546`/`542`/`545` already use, and mean
    # the same thing here, so the values are reused verbatim rather than
    # re-typed.
    "double_chance_and_first_half_btts": _f(sides=SIDES_DOUBLE_CHANCE_AND_BTTS),
    "double_chance_and_second_half_btts": _f(sides=SIDES_DOUBLE_CHANCE_AND_BTTS),
    # `1724`-`1729` at match level (`547`); `145`-`150` on sportybet's private
    # half-twins (`900042`/`900043`, task 0005e) — disjoint ids, one family.
    "double_chance_and_total_goals": _f(
        line_keys=("total",),
        sides={**SIDES_DOUBLE_CHANCE_AND_TOTAL, **SIDES_DOUBLE_CHANCE_AND_TOTAL_SPORTYBET_HALF},
    ),
    "halftime_fulltime_and_total_goals": _f(
        line_keys=("total",), sides=SIDES_HALFTIME_FULLTIME_AND_TOTAL
    ),
    "halftime_fulltime_and_first_half_total_goals": _f(
        line_keys=("total",), sides=SIDES_HALFTIME_FULLTIME_AND_TOTAL
    ),
    # `820` "Halftime/Fulltime & Exact Goals" (task 0007b) — HT/FT result
    # crossed with an exact match goals bucket. Same regular-time reasoning
    # as `818`/`819` (D26(c)): both legs settle on the regular-time result,
    # `time_basis = regular` gives the calculator the h1 and reg scores the
    # market needs. Its own family — the outcome id block (`1854`-`1889`)
    # collides with nothing else.
    "halftime_fulltime_and_exact_goals": _f(sides=SIDES_HALFTIME_FULLTIME_AND_EXACT_GOALS),
    "no_draw_and_btts": _f(sides=SIDES_YES_NO_SPORTYBET),
    # `55` "1st/2nd Half Both Teams To Score" (task 0007b) — a single bet on
    # the (1h_btts, 2h_btts) pair. Regular time: both halves' BTTS flags are
    # read from D18's period-resolved scores, same as `47` reads h1/h2 for
    # HT/FT. Distinct from `75`/`95` (bare 1h/2h BTTS) — this one is a joint
    # outcome, not two independent bets.
    "first_and_second_half_btts": _f(sides=SIDES_FIRST_AND_SECOND_HALF_BTTS),
    # `184` "Xth Goal & 1X2" (task 0007b) — combination of the goal identity
    # (home/away) with the match 1X2. `goalnr` picks *which* goal, so it lives
    # on `side_or_line` just as it does for `next_goal` (`8`) and `goalscorer_nth`
    # (`38`). Regular time.
    "xth_goal_and_1x2": _f(line_keys=("goalnr",), sides=SIDES_XTH_GOAL_AND_1X2),
    # `1179` "1st Half Result or Match Result" (task 0014) — a compound bet
    # that wins if EITHER the 1st-half result OR the match result matches the
    # picked side. Outcome ids are `1`/`2`/`3` — the SAME `SIDES_1X2` block the
    # bare match 1X2 uses (`1` in the payloads). Regular time by D26(c): the
    # match-result leg needs the regular-time score, and D18 stores h1 alongside
    # reg so the calculator has both components; `1h` would drop the match leg.
    "first_half_or_match_result": _f(sides=SIDES_1X2),
    # `166` "Corners O/U" (task 0014) — total corners bet, `total` specifier,
    # betradar `12`/`13` over/under ids. All three books publish the same id
    # block; sportybet's team-corner markets (`900300`+) use its private
    # `30`/`31` block instead — a different family, not a period of this one.
    "total_corners": _f(line_keys=("total",), sides=SIDES_OVER_UNDER),
    # `854`-`859` "1X2 or Total" (task 0014) — six compound markets that win if
    # the picked 1X2 side OR the picked over/under leg wins. Outcomes are
    # yes/no (`74`/`76`); the `total` specifier picks the line. Each is its own
    # family per D5 (`19`/`20`/`27`/`28` precedent): the 1X2 side and the O/U
    # direction are *market* axes, not outcome axes. Regular time.
    "home_or_over": _f(line_keys=("total",), sides=SIDES_YES_NO),
    "home_or_under": _f(line_keys=("total",), sides=SIDES_YES_NO),
    "draw_or_over": _f(line_keys=("total",), sides=SIDES_YES_NO),
    "draw_or_under": _f(line_keys=("total",), sides=SIDES_YES_NO),
    "away_or_over": _f(line_keys=("total",), sides=SIDES_YES_NO),
    "away_or_under": _f(line_keys=("total",), sides=SIDES_YES_NO),
    # `860`-`862` "1X2 or Both Teams To Score" (task 0014) — same D5 shape as
    # the O/U twins above but with the second leg being BTTS-yes rather than an
    # O/U threshold, so no `total` specifier. Yes/no outcomes.
    "home_or_btts": _f(sides=SIDES_YES_NO),
    "draw_or_btts": _f(sides=SIDES_YES_NO),
    "away_or_btts": _f(sides=SIDES_YES_NO),
    # `863`-`865` "1X2 or Any Clean Sheet" (task 0014) — "Any Clean Sheet"
    # meaning either team ends the match without conceding, i.e. `31 OR 32`
    # at the outcome grain. Compound with 1X2, yes/no outcomes, regular time.
    # Distinct from `clean_sheet_home` (id `31`) / `clean_sheet_away` (id `32`)
    # because the leg is *any*, not a specific team.
    "home_or_any_clean_sheet": _f(sides=SIDES_YES_NO),
    "draw_or_any_clean_sheet": _f(sides=SIDES_YES_NO),
    "away_or_any_clean_sheet": _f(sides=SIDES_YES_NO),
    # `770`/`775`/`776`/`777`/`800117` (task 0014) — sportybet player-prop
    # markets whose outcomes are all `pre:playerprops:<event>:<player>[:N]`
    # structured ids (`is_structured_outcome_id` catches them, so the id
    # lands verbatim in `side_or_line`). Empty `sides` because every outcome
    # is a structured id — the same shape `800097`/`800109` already use, and
    # the same D25(b) obligation applies: the D13 crosswalk task will DELETE-
    # then-INSERT these rows when it fills `player_key`, because filling it
    # mutates the natural key. No `line_keys`: the `variant` specifier
    # carries the player identity but so does the outcome id itself, and
    # declaring it as a line would spell the player twice into
    # `side_or_line`, doubling every player's `dim_outcome` row and never
    # matching an upsert.
    "player_assists_incl_overtime": _f(),
    "player_goals_incl_overtime": _f(),
    "player_shots_incl_overtime": _f(),
    "player_shots_on_goal_incl_overtime": _f(),
    "player_to_be_booked": _f(),
    # --- player props (D13: resolved, never crosswalked) --------------------
    # No `sides` beyond `1716`: every other outcome is an `sr:player:` id and
    # takes the structured path, which is what keeps the players distinct under
    # `NULLS NOT DISTINCT` while `player_key` stays NULL.
    "goalscorer_nth": _f(
        line_keys=("goalnr",),
        sides=SIDES_GOALSCORER,
        note="`goalnr` picks which goal; `type` (prematch/live) is not a line.",
    ),
    "goalscorer_last": _f(sides=SIDES_GOALSCORER),
    "goalscorer_anytime": _f(sides=SIDES_GOALSCORER),
    "player_to_score_two_plus": _f(),
    "player_to_score_three_plus": _f(),
    # `800097`/`800109` (task 0007b) — sportybet's player-prop markets whose
    # outcomes are all `pre:playerprops:<event>:<player>[:N[:team]]` ids. Empty
    # `sides` because every outcome is a structured id (auto-resolved by
    # `is_structured_outcome_id`); the raw id in `side_or_line` keeps the
    # players distinct under `NULLS NOT DISTINCT`, exactly the trap this
    # module's docstring warns about. `player_key` stays NULL until D13.
    "player_fouls_won": _f(),
    "player_not_to_score": _f(),
    # --- minute-window markets (D26(b)'s `other`) ---------------------------
    # The window is a specifier, so it is a **line** and lives here. That is
    # what lets the family name stay window-agnostic: one `dim_market` row for
    # "1X2 over a minute range", one `dim_outcome` row per window.
    "1x2_minute_range": _f(line_keys=("minute",), sides=SIDES_1X2),
    "total_goals_minute_range": _f(
        line_keys=("minute", "total"),
        sides=SIDES_OVER_UNDER_SPORTYBET_MINUTE,
        note="sportybet uses `30`/`31` here, not betradar's `12`/`13`.",
    ),
    "1x2_ten_minute_interval": _f(line_keys=("from", "to"), sides=SIDES_1X2),
    "total_goals_early_goals": _f(
        line_keys=("minsnr", "total"),
        sides=SIDES_OVER_UNDER,
        note="`minsnr` is the minute the early payout triggers on.",
    ),
    "1x2_one_up": _f(sides=SIDES_1X2),
    # The interval **width** is fixed by the betradar id (15 min for `100`,
    # 10 min for `101`) rather than sampled from a specifier, and their outcome
    # id blocks are disjoint — so these stay two families and their names name a
    # market property, not a sampled window.
    "goal_minute_interval_15": _f(line_keys=("goalnr",), sides=SIDES_GOAL_MINUTE_INTERVAL_15),
    "goal_minute_interval_10": _f(line_keys=("goalnr",), sides=SIDES_GOAL_MINUTE_INTERVAL_10),
}


def lookup(market_family: str) -> FamilyOutcomes | None:
    """The outcome rules for a family, or None if the table does not cover it.

    None is the honest answer and the caller must treat it as one: count it,
    log it, write no row. It is never a guessed side.
    """
    return FAMILY_OUTCOMES.get(market_family)


def is_player_outcome_id(outcome_id: str) -> bool:
    """True for betradar's player outcome ids — structural, never by label.

    `1716` ("No Goal") sits in a goalscorer market and is **not** a player; it
    resolves through the ordinary side table.
    """
    return any(outcome_id.startswith(prefix) for prefix in PLAYER_ID_PREFIXES)


def is_structured_outcome_id(outcome_id: str) -> bool:
    """True for betradar's self-describing ids (`sr:exact_goals:3+:88`).

    A colon is the whole test: betradar's bare side ids are decimal integers and
    its structured ids are all `namespace:…` forms.
    """
    return ":" in outcome_id


def canonical_line_value(value: str) -> str:
    """One specifier value, canonicalised for `side_or_line`.

    Numeric values are reduced to their shortest exact decimal form so that
    `2.50`, `+2.5` and `2,5` are one line and not three. Anything non-numeric —
    a handicap pair `0:1`, a minute `1-85` — passes through lowercased, because
    reinterpreting it would be exactly the silent reinterpretation D16 forbids.
    """
    text = value.strip().lower().replace(",", ".")
    if text.startswith("+"):
        text = text[1:]
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    normalised = number.normalize()
    # `Decimal("100").normalize()` is `1E+2`; `quantize` back to a plain form.
    if normalised == normalised.to_integral_value():
        normalised = normalised.quantize(Decimal(1))
    return format(normalised, "f")


def compose_side_or_line(side: str, line_values: tuple[str, ...]) -> str:
    """`side` plus the family's canonicalised line, in the documented format."""
    if not line_values:
        return side
    line = LINE_VALUE_SEPARATOR.join(canonical_line_value(value) for value in line_values)
    return f"{side}{LINE_SEPARATOR}{line}"
