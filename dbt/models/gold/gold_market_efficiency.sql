-- How far apart the books are, by week and market family.
--
-- NOT per-bookmaker vig, which the plan originally called for and which this
-- pipeline cannot honestly produce: a book's margin needs that book's COMPLETE
-- market, and fact_arbitrage_signal stores the best price per outcome ACROSS
-- books. What it can measure -- and what a cross-bookmaker platform should
-- measure -- is the overround left after shopping every book:
--
--     overround = 1 / arbitrage - 1
--
-- Zero means the best available prices exactly cancel. Negative is a true
-- surebet. Positive is the shortfall a punter pays even at the best prices,
-- and a market whose overround trends toward zero is one the books disagree
-- about.
--
-- Reported twice: over every signal, and over the fresh ones only. The gap
-- between those two columns is the cost of stale legs, stated rather than
-- filtered away.

with signals as (
    select
        s.*,
        coalesce(m.market_name, 'market ' || s.market_base_id) as market_name,
        date_trunc('week', s.detected_at)                      as week
    from {{ ref('stg_arbitrage_signal') }} s
    left join {{ source('core', 'dim_market') }} m
        on m.market_base_id = s.market_base_id
)

select
    week,
    market_name,
    market_base_id,

    count(*)                                    as signals,
    count(distinct event_id)                    as fixtures,
    sum(case when is_surebet then 1 else 0 end) as surebets,

    avg(overround)                              as mean_overround,
    min(overround)                              as best_overround,

    sum(case when is_fresh then 1 else 0 end)   as fresh_signals,
    avg(case when is_fresh then overround end)  as mean_overround_fresh,

    avg(leg_spread_seconds)                     as mean_leg_spread_seconds

from signals
group by week, market_name, market_base_id
