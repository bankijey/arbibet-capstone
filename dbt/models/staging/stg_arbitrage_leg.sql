-- One row per LEG of an arbitrage signal, rather than one per signal.
--
-- `fact_arbitrage_signal.legs` is JSON because legs are only ever read as
-- a set -- flattening them in the fact would repeat the arbitrage value on
-- every row. But the dashboard wants to SHOW the set: which book priced which
-- outcome, at what price, so a reader can see the shape of the opportunity
-- rather than a single number asserting one existed.
--
-- The unnest is its own CTE so the joins below read against plain rows.
--
-- `leg_spread_seconds` is the gap between the OLDEST and NEWEST price in the
-- set -- how far from simultaneous the "snapshot" was. It is surfaced as
-- "spread" rather than "lag": lag suggests a delay between us and the market,
-- and this is disagreement between the books' own clocks.

with flattened as (
    select
        s.signal_key,
        s.event_id,
        s.market_id,
        s.market_base_id,
        s.specifier,
        s.arbitrage,
        s.n_legs,
        s.leg_spread_seconds,
        s.oldest_leg_fire_time,
        s.newest_leg_fire_time,
        s.detected_at,
        leg.value ->> '$.outcome_id'           as outcome_id,
        leg.value ->> '$.bookmaker'            as bookmaker_name,
        (leg.value ->> '$.odds')::double       as odds
    from {{ source('core', 'fact_arbitrage_signal') }} s,
         unnest(cast(s.legs as json[])) as leg(value)
    -- Whole signals whose any leg is from a mismatched book are dropped, as in
    -- stg_arbitrage_signal.
    where s.signal_key in (select signal_key from {{ ref('stg_arbitrage_signal') }})
)

select
    l.signal_key,
    l.event_id,
    l.market_id,
    l.specifier                                          as line,
    l.arbitrage,
    -- Same definitions as stg_arbitrage_signal; see there and FINDINGS 13g.
    l.arbitrage > 1
      and l.leg_spread_seconds <= {{ var('max_leg_spread_seconds') }}
                                                         as is_surebet,
    l.leg_spread_seconds <= {{ var('max_leg_spread_seconds') }}
                                                         as is_fresh,
    l.n_legs,
    l.leg_spread_seconds                                 as spread_seconds,
    {{ platform_time('l.detected_at') }} as detected_at,

    -- Fixtures roll out of the loader's window while signals do not, so a
    -- signal can outlive the fixture row it points at. LEFT, and falling back
    -- to the id: a detected opportunity must never vanish from the page
    -- because its dimension aged out.
    coalesce(f.home_team || ' v ' || f.away_team, l.event_id) as fixture,
    {{ platform_time('f.kickoff_at') }} as kickoff_at,
    coalesce(m.market_name, 'market ' || l.market_base_id)    as market_name,

    l.outcome_id,
    -- Three sources, most authoritative first. The settlement taxonomy names
    -- an outcome only for markets the engine can settle; the books name every
    -- outcome they price; and the raw id is the last resort. See
    -- stg_outcome_label -- resolving an id WITHOUT its market would be wrong,
    -- because betradar outcome ids are market-scoped.
    coalesce(o.side, lab.label, 'outcome ' || l.outcome_id)   as outcome,
    l.bookmaker_name,
    l.odds

from flattened l
left join {{ source('core', 'dim_fixture') }} f on f.event_id = l.event_id
left join {{ source('core', 'dim_market') }} m on m.market_base_id = l.market_base_id
left join {{ source('core', 'dim_market_outcome') }} o
       on o.market_id = l.market_base_id::varchar
      and o.outcome_id = l.outcome_id
left join {{ ref('stg_outcome_label') }} lab
       on lab.market_id = l.market_id
      and lab.outcome_id = l.outcome_id
