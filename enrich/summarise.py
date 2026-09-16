"""Write an LLM verdict for each booking slip into `gold_slip_summary_ai`.

Reads `gold_slip_leg_history`, so every number the model sees is one the
warehouse can show its working for. Skips slips whose exact leg-set already has
a summary, and stops at `SUMMARISE_LIMIT` slips per run -- an unbounded loop
over a growing slip table is how twenty cents becomes twenty dollars.

Run:
    python enrich/summarise.py

Scope (SUMMARISE_SCOPE):
    upcoming   slips with at least one leg still to kick off -- the half-hourly
               signals DAG runs this, because an upcoming slip is only worth a
               verdict while it can still be placed
    played     slips whose every leg has kicked off -- the daily history DAG
    all        both, budget split between them (default; for manual runs)
"""

from __future__ import annotations

import logging
import os

from openai import OpenAI

from arbibet_capstone.env import load as load_env
from arbibet_capstone.env import require
from arbibet_capstone.summarise import MODEL, Slip, build_prompt, slips_from_rows
from arbibet_capstone.warehouse import connect, merge_bulk

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("summarise")

TABLE = "gold_slip_summary_ai"

# Most-followed first. If the budget only covers twenty slips, they should be
# the twenty the most people copied.
#
# The combined-probability arithmetic is done HERE, in the warehouse, not in
# Python and never by the model. `combined_odds` is the product of every leg's
# price -- exp(sum(ln())), because Snowflake has no product aggregate and the
# numbers reach nine figures. Its reciprocal is the whole slip's implied
# probability, and `one_in_n` is that expressed as "1 in N" for the opener. The
# model is handed these as finished figures and only phrases them.
_LEGS = """
    WITH leg AS (
        SELECT share_code, followed_times, home_team, away_team, tournament, market_name,
               market_family, outcome_name, side_or_line, odds, implied_rate,
               historical_rate, leg_index,
               -- The counts that belong to that rate. The gold model picks
               -- them from the same branch; choosing here would be a second
               -- place for the two to disagree.
               history_wins    AS wins,
               history_matches AS matches
        FROM ANALYTICS.gold_slip_leg_history
    ),
    slip AS (
        -- odds > 0 guards ln(); real prices are always positive, but a
        -- defensive filter beats a whole slip's combined odds going NULL.
        SELECT share_code, exp(sum(ln(odds))) AS combined_odds
        FROM leg WHERE odds > 0 GROUP BY share_code
    )
    SELECT leg.*,
           slip.combined_odds,
           1 / slip.combined_odds        AS combined_probability,
           -- "1 in N" is only meaningful once a slip is actually unlikely.
           -- round(1.19) = 1, and "1 in 1" reads as certainty while inviting
           -- the model to manufacture a rarity claim for an 84%-likely slip.
           -- NULL below 2.0 says "this is a likely combination", and the
           -- prompt phrases that case differently.
           CASE WHEN slip.combined_odds >= 2
                THEN round(slip.combined_odds) END  AS one_in_n
    FROM leg JOIN slip USING (share_code)
    ORDER BY followed_times DESC NULLS LAST, share_code, leg_index
"""

# Upcoming = at least one leg has not kicked off. A slip whose fixtures have all
# started is a historical record; one with anything still to play is a bet
# someone can still be holding. NULL kick-offs (a fixture that rolled out of
# dim_fixture) count as played: a fixture old enough to have rolled out is not
# upcoming.
_UPCOMING = """
    SELECT share_code,
           max(IFF(kickoff_at > current_timestamp(), 1, 0)) = 1 AS upcoming
    FROM ANALYTICS.gold_slip_leg_history
    GROUP BY share_code
"""


def main() -> int:
    limit = int(os.environ.get("SUMMARISE_LIMIT", "20"))
    scope = os.environ.get("SUMMARISE_SCOPE", "all")
    if scope not in ("upcoming", "played", "all"):
        # A typo here must not silently fall through to "all" and spend the
        # half-hourly budget on played slips.
        log.error("SUMMARISE_SCOPE must be upcoming, played or all, not %r", scope)
        return 1
    client = OpenAI(api_key=require("OPENAI_KEY"))

    with connect() as warehouse:
        with warehouse.cursor() as cur:
            cur.execute(_LEGS)
            columns = [c[0] for c in cur.description]
            rows = [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]

            # The newest verdict per slip, and what it was written about.
            cur.execute(
                f"""
                SELECT share_code, leg_signature, structure_signature
                FROM CORE.{TABLE}
                QUALIFY row_number() OVER (
                    PARTITION BY share_code ORDER BY generated_at DESC
                ) = 1
                """
            )
            latest = {code: (legs, structure) for code, legs, structure in cur.fetchall()}

            cur.execute(_UPCOMING)
            upcoming = {code for code, is_upcoming in cur.fetchall() if is_upcoming}

        slips = slips_from_rows(rows)

        # Never-summarised slips FIRST, then slips whose legs or evidence
        # changed; each group most-copied first (the SQL order, kept by a
        # stable sort). Price drift alone is not a change -- see
        # Slip.structure_signature for why, and the cost of treating it as one.
        def need(slip: Slip) -> int | None:
            if slip.share_code not in latest:
                return 0
            legs_sig, structure_sig = latest[slip.share_code]
            if structure_sig is None:
                # Written before structure_signature existed: fall back to the
                # exact comparison once, after which the structure is stored.
                return None if legs_sig == slip.signature() else 1
            return None if structure_sig == slip.structure_signature() else 1

        ranked = [(need(s), s) for s in slips]
        pending = [
            s
            for rank, s in sorted(
                ((r, s) for r, s in ranked if r is not None), key=lambda pair: pair[0]
            )
        ]
        new = sum(1 for r, _ in ranked if r == 0)
        log.info(
            "slips=%d up to date=%d pending=%d (never summarised=%d, changed=%d)",
            len(slips),
            len(slips) - len(pending),
            len(pending),
            new,
            len(pending) - new,
        )

        # The budget is SPLIT between upcoming and played slips, each most-
        # followed first. One ranking over both starved the upcoming half
        # completely: the most-copied slips are old ones, because copies
        # accumulate over time, so a slip posted this morning never outranked
        # them. On 2026-09-15 there were 166 upcoming slips and not one had a
        # summary. Whatever one side does not use goes to the other.
        ahead = [x for x in pending if x.share_code in upcoming]
        behind = [x for x in pending if x.share_code not in upcoming]
        if scope == "upcoming":
            batch = ahead[:limit]
            take_ahead = len(batch)
        elif scope == "played":
            batch = behind[:limit]
            take_ahead = 0
        else:
            take_ahead = min(len(ahead), max(limit - len(behind), limit // 2))
            batch = ahead[:take_ahead] + behind[: limit - take_ahead]
        log.info(
            "scope=%s this run: upcoming=%d played=%d (pending upcoming=%d played=%d)",
            scope,
            take_ahead,
            len(batch) - take_ahead,
            len(ahead),
            len(behind),
        )

        written: list[dict[str, object]] = []
        failed = 0
        for slip in batch:
            try:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=build_prompt(slip),  # type: ignore[arg-type]
                    max_tokens=180,
                    temperature=0.3,
                )
            except Exception:
                # One slip's failure is not the run's. Logged with a traceback
                # so a rate limit is distinguishable from a bad prompt.
                log.error("slip %s failed", slip.share_code, exc_info=True)
                failed += 1
                continue

            text = (response.choices[0].message.content or "").strip()
            if not text:
                log.warning("slip %s returned an empty summary", slip.share_code)
                failed += 1
                continue

            written.append(
                {
                    "share_code": slip.share_code,
                    "leg_signature": slip.signature(),
                    "structure_signature": slip.structure_signature(),
                    "summary": text,
                    "model": MODEL,
                    "legs": len(slip.legs),
                    "legs_with_history": slip.legs_with_history,
                    "followed_times": slip.followed_times,
                }
            )

        if written:
            merge_bulk(
                warehouse,
                table=TABLE,
                rows=written,
                key=["share_code", "leg_signature"],
            )

    log.info("summarised=%d failed=%d", len(written), failed)
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
