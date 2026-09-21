"""Pass 1 of settlement: a verdict minutes after full time, from the book's own result.

WHY. API-Football results arrive once a day and only five market families were
ever settled from them, so 60% of EV signals and a quarter of slip legs never
got a result, and the rest waited until the next morning.

WHERE THE RESULT COMES FROM. Neither collector holds it: markets bronze stops
polling at kick-off, and the live collector follows a handful of hand-picked
competitions. But msport's match-detail endpoint -- the one the markets
collector already calls for prices -- keeps answering after full time, for at
least a day: `eventMatchStatus: "Ended"`, `scoreOfWholeMatch: "2:1"`,
`scoreOfSection: ["1:0"]` (the first half). That is enough for every market a
scoreline decides: the settlement engine takes period scores and a market's
family, period, time basis and `side@line`.

WHAT IS SETTLED. Not every market of every match: only what something asked
about -- the outcomes of EV signals, surebet legs, slip legs and wallet bets
(odds/settle_fast.py builds that demand). Thousands of rows, not millions.

HOW CAREFUL. A verdict here is PROVISIONAL; pass 2 (API-Football) confirms or
corrects it. And this pass refuses rather than guesses:

* only `Ended`, never a match that is merely no longer offered;
* only normal-time matches: one completed section (the first half) whose score
  fits inside the final score. Extra time and penalties change which score a
  market settles on, and msport's sections do not say which is which;
* only when the teams msport names are the fixture's teams. A merged fixture is
  exactly the case where the book reports another match's score.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from arbibet_capstone.crosswalk.settle.settlement import (
    PeriodResolvedScores,
    TeamScore,
    settle,
)
from arbibet_capstone.verify import CLEAR_MATCH, similarity

log = logging.getLogger("arbibet_capstone.fast_settle")

SOURCE = "msport"
MSPORT_DETAIL = "https://www.msport.com/api/ng/facts-center/query/frontend/match/detail?eventId="
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# What something asked about, in the engine's terms: a CTE both passes start
# from. `time_basis` says which score a market settles on; it lives on
# dim_market_outcome.
ASKED = """
    asked AS (
        SELECT e.event_id, o.market_family, o.period, o.time_basis,
               if(o.has_line, o.side || '@' || e.specifier, o.side) AS side_or_line
        FROM core.fact_ev_signal e
        JOIN core.dim_market_outcome o
          ON o.market_id = e.market_base_id::varchar AND o.outcome_id = e.outcome_id
        UNION
        SELECT s.event_id, o.market_family, o.period, o.time_basis,
               if(o.has_line, o.side || '@' || s.specifier, o.side)
        FROM core.fact_arbitrage_signal s,
             unnest(cast(s.legs AS json[])) AS leg(value)
        JOIN core.dim_market_outcome o
          ON o.market_id = s.market_base_id::varchar
         AND o.outcome_id = json_extract_string(leg.value, '$.outcome_id')
        WHERE s.arbitrage > 1
        UNION
        SELECT l.event_id, l.market_family, l.period, o.time_basis, l.side_or_line
        FROM analytics.stg_slip_leg l
        JOIN core.dim_market_outcome o
          ON o.market_id = l.market_id AND o.outcome_id = l.outcome_id
        WHERE l.event_id IS NOT NULL AND l.side_or_line IS NOT NULL
    )
"""

# An outcome a wallet bet names, in the engine's terms.
_OUTCOME = """
    SELECT o.market_family, o.period, o.time_basis, o.side, o.has_line
    FROM core.dim_market_outcome o
    WHERE o.market_id = %s AND o.outcome_id = %s
"""


def wallet_demand(warehouse: Any, since_days: int | None = None) -> list[dict[str, Any]]:
    """Wallet bets' outcomes: open ones, and with `since_days` those settled lately
    too (pass 2 confirms what they were paid on). Best-effort: no bot, no demand."""
    from datetime import UTC, datetime, timedelta

    try:
        from runner import telegram
        from runner.telegram.store import Store

        if not telegram.configured():
            return []
        store = Store()
        bets = store.open_bets(datetime.now(UTC))
        if since_days is not None:
            bets += store.settled_bets(datetime.now(UTC) - timedelta(days=since_days))
    except Exception:
        log.warning("wallet demand unavailable", exc_info=True)
        return []
    rows: list[dict[str, Any]] = []
    with warehouse.cursor() as cur:
        for bet in bets:
            base, _, specifier = str(bet["market_id"]).partition(";")
            for leg in bet["legs"]:
                cur.execute(_OUTCOME, (base, str(leg.get("outcomeId"))))
                found = cur.fetchone()
                if not found:
                    continue
                family, period, basis, side, has_line = found
                rows.append(
                    {
                        "event_id": str(bet["event_id"]),
                        "market_family": family,
                        "period": period,
                        "time_basis": basis,
                        "side_or_line": f"{side}@{specifier}" if has_line else side,
                    }
                )
    return rows


# msport's `eventMatchStatus`, reduced to what this pass cares about.
_STATUS = {
    "ended": "ended",
    "not start": "not_started",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "postponed": "postponed",
    "abandoned": "abandoned",
    "interrupted": "interrupted",
    "suspended": "interrupted",
}


@dataclass(frozen=True)
class EventResult:
    status: str  # ended | not_started | live | cancelled | postponed | abandoned | ...
    raw_status: str | None
    full_time: tuple[int, int] | None
    sections: tuple[tuple[int, int], ...]
    home: str | None
    away: str | None

    @property
    def half_time(self) -> tuple[int, int] | None:
        return self.sections[0] if self.sections else None

    def score_text(self) -> str | None:
        if self.full_time is None:
            return None
        text = f"{self.full_time[0]}:{self.full_time[1]}"
        if self.half_time is not None:
            text += f" (HT {self.half_time[0]}:{self.half_time[1]})"
        return text


def _pair(text: Any) -> tuple[int, int] | None:
    if not isinstance(text, str) or ":" not in text:
        return None
    home, _, away = text.partition(":")
    try:
        return int(home.strip()), int(away.strip())
    except ValueError:
        return None


def parse_msport(payload: Mapping[str, Any]) -> EventResult | None:
    """msport's match-detail response -> what it says about the match. None if unreadable."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return None
    raw = data.get("eventMatchStatus")
    status = _STATUS.get(str(raw).strip().lower()) if raw is not None else None
    if status is None:
        status = "unknown" if raw is None else "live"
    sections_raw = data.get("scoreOfSection")
    if isinstance(sections_raw, str):
        try:
            sections_raw = json.loads(sections_raw)
        except ValueError:
            sections_raw = None
    sections = tuple(pair for pair in (_pair(s) for s in (sections_raw or [])) if pair is not None)
    return EventResult(
        status=status,
        raw_status=None if raw is None else str(raw),
        full_time=_pair(data.get("scoreOfWholeMatch")),
        sections=sections,
        home=data.get("homeTeam"),
        away=data.get("awayTeam"),
    )


def names_agree(result: EventResult, home: str | None, away: str | None) -> bool:
    """The teams msport names are the fixture's. Anything less and the score is not ours."""
    return (
        similarity(home, result.home) >= CLEAR_MATCH
        and similarity(away, result.away) >= CLEAR_MATCH
    )


def trusted(
    result: EventResult,
    home: str | None,
    away: str | None,
    listing: tuple[str, str | None, str | None] | None = None,
    flagged: bool = False,
) -> bool:
    """Whether the score may be used for this fixture.

    Yes when msport names the fixture's teams. Also yes when the match check
    (core.fixture_check) accepted msport's pre-match listing for the fixture --
    an alias or a rebrand the name comparison cannot see, settled there by the
    other books agreeing or by a person -- and the result names that same
    listing. Never for a fixture flagged as a wrong match, and never on a
    listing still awaiting review.

    `listing`: (verdict, home, away) of msport's row in fixture_check.
    """
    if flagged:
        return False
    if names_agree(result, home, away):
        return True
    if listing is None or listing[0] not in ("ok", "cleared"):
        return False
    return names_agree(result, listing[1], listing[2])


def period_scores(result: EventResult) -> tuple[PeriodResolvedScores | None, str | None]:
    """Period scores for the engine, or (None, why not).

    Only a normal-time match: exactly one completed section (the first half)
    that fits inside the final score. Then regulation = full = final, and the
    second half is the difference.
    """
    if result.status != "ended":
        return None, f"status is {result.status}"
    if result.full_time is None:
        return None, "no final score"
    if len(result.sections) != 1:
        return None, f"{len(result.sections)} sections: extra time or an unusual match"
    (h1_home, h1_away), (ft_home, ft_away) = result.sections[0], result.full_time
    if h1_home > ft_home or h1_away > ft_away:
        return None, "half-time score exceeds the final score"
    final = TeamScore(ft_home, ft_away)
    return (
        PeriodResolvedScores(
            h1=TeamScore(h1_home, h1_away),
            h2=TeamScore(ft_home - h1_home, ft_away - h1_away),
            reg=final,
            full=final,
        ),
        None,
    )


def settle_demand(
    result: EventResult, demand: Iterable[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Verdicts for the demanded markets of one ended fixture.

    `demand`: rows with market_family, period, time_basis, side_or_line. An
    `unsettleable` verdict is not written -- it would only block pass 2 -- but
    its reason is counted, so what the engine cannot do stays visible.
    """
    scores, why = period_scores(result)
    refusals: Counter[str] = Counter()
    if scores is None:
        refusals[why or "no scores"] += 1
        return [], refusals
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in demand:
        key = (str(row["market_family"]), str(row["period"]), str(row["side_or_line"]))
        if key in seen:
            continue
        seen.add(key)
        verdict = settle(
            scores, "ft", key[0], key[1], str(row.get("time_basis") or "regular"), key[2]
        )
        if verdict.verdict == "unsettleable":
            refusals[f"{key[0]}/{key[1]}: {verdict.reason}"] += 1
            continue
        out.append(
            {
                "market_family": key[0],
                "period": key[1],
                "side_or_line": key[2],
                "time_basis": str(row.get("time_basis") or "regular"),
                "verdict": verdict.verdict,
                "reason": verdict.reason,
            }
        )
    return out, refusals


def fetch_msport(http: Any, sr_match_id: str) -> dict[str, Any] | None:
    """One GET to msport's match detail. None on any failure: the next cycle asks again."""
    try:
        response = http.get(MSPORT_DETAIL + quote(sr_match_id, safe=""))
        if response.status_code != 200:
            return None
        body = response.json()
    except Exception as err:  # network, JSON, anything: this pass is best-effort
        log.debug("msport result fetch failed for %s: %s", sr_match_id, err)
        return None
    return body if isinstance(body, dict) else None
