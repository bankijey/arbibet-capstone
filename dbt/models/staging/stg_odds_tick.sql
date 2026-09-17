-- Price changes, labelled and masked, ready to chart.
--
-- `fact_odds_tick.market_id` is the CANONICAL id and carries its specifier
-- ('18;2.5'), because that is what identifies a tradeable market -- Over/Under
-- 2.5 and Over/Under 3.5 are different bets. `dim_market` is keyed on the BASE
-- id without the specifier, so the join splits it and the line is kept as its
-- own column.
--
-- Every label here is a LEFT join with a fallback. A tick is an observation
-- that happened; it must not disappear from a chart because a dimension has
-- since rolled or because the settlement taxonomy never named the outcome.

with tick as (
    select
        t.event_id,
        t.market_id,
        split_part(t.market_id, ';', 1)                as market_base_id,
        nullif(split_part(t.market_id, ';', 2), '')    as line,
        t.outcome_id,
        t.bookmaker_id,
        t.odds,
        t.fire_time
    from {{ source('core', 'fact_odds_tick') }} t
)

select
    t.event_id,
    t.market_id,
    t.line,
    t.outcome_id,
    t.odds,
    {{ platform_time('t.fire_time') }} as fire_time,

    coalesce(f.home_team || ' v ' || f.away_team, t.event_id) as fixture,
    coalesce(m.market_name, 'market ' || t.market_base_id)    as market_name,

    -- See FINDINGS 12c: outcome ids are MARKET-SCOPED. Id 12 means `over` in
    -- one market and `1-2` in another, so the join carries both halves of the
    -- key and the fallback prints the raw id rather than guessing.
    coalesce(o.side, 'outcome ' || t.outcome_id)              as outcome,

    b.bookmaker_name

from tick t
left join {{ source('core', 'dim_fixture') }} f on f.event_id = t.event_id
left join {{ source('core', 'dim_bookmaker') }} b on b.bookmaker_id = t.bookmaker_id
left join {{ source('core', 'dim_market') }} m
       on m.market_base_id = try_cast(t.market_base_id as bigint)
left join {{ source('core', 'dim_market_outcome') }} o
       on o.market_id = t.market_base_id
      and o.outcome_id = t.outcome_id
