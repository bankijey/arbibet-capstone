-- A human name for every (market, outcome) we have ever observed a price on.
--
-- WHY THIS EXISTS. `dim_market_outcome` is materialised from the settlement
-- engine's maps, and the engine settles from SCORES. Markets it cannot settle
-- -- corners above all -- are absent from it entirely, so a leg on "1st half
-- Corners O/U" had an id and no name, and the surebets table displayed
-- `outcome 12`. Both of the best surebets found are on corners, so the single
-- most prominent table on the dashboard was the one showing raw ids.
--
-- WHY NOT JUST MAP THE ID. Betradar outcome ids are MARKET-SCOPED. Across the
-- taxonomy, id 12 means `over` in one market and `1-2` in another, and id 13
-- has three distinct meanings. Resolving an id on its own would print a
-- confident, plausible, wrong label -- the failure mode this project keeps
-- rediscovering.
--
-- WHERE THE NAMES COME FROM. The books publish them. Every parser already
-- carries `Outcome.name` through from the payload, and `odds/ticks.py` stores
-- it, so the label is the bookmakers' own word for the outcome rather than
-- anyone's inference. Where books disagree, the most frequently published name
-- wins; ties break alphabetically so the model is deterministic.
--
-- COVERAGE. Only markets with extracted ticks, which is by construction every
-- market that produced a signal plus the deep-dive markets. A market with no
-- ticks still falls back to `outcome <id>` downstream, and that is correct: we
-- have never seen anyone price it, so we have no name for it.

with named as (
    select
        market_id,
        outcome_id,
        outcome_name,
        count(*) as times_published
    from {{ source('core', 'fact_odds_tick') }}
    where outcome_name is not null
      and trim(outcome_name) != ''
    group by 1, 2, 3
)

select
    market_id,
    outcome_id,
    outcome_name as label,
    times_published
from named
qualify row_number() over (
    partition by market_id, outcome_id
    order by times_published desc, outcome_name
) = 1
