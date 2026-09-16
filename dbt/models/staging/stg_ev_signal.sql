-- Positive-EV signals, one row per outcome at one book.
--
-- `implied_p` is BORROWED: only sportybet and msport publish a probability
-- with their prices, so every other book's EV is measured against someone
-- else's model. `p_source` says whose, and `is_own_probability` says whether
-- the book was judged by its own number or a rival's -- a distinction that
-- changes how much weight the signal deserves.
--
-- The probability is the MOST RECENT one sportybet or msport published, and it
-- must be within `max_leg_spread_seconds` of the price (refused otherwise, at
-- detection). `is_fresh` is NULL, not FALSE, on rows from before that rule:
-- their probability's age was never recorded, so it is unknown rather than
-- stale. Anything offered as still placeable must require `is_fresh`.

select
    e.signal_key,
    e.event_id,
    e.market_id,
    e.market_base_id,
    e.specifier,
    e.outcome_id,
    e.outcome_name,
    b.bookmaker_name,
    e.odds,
    e.implied_p,
    e.p_source,
    e.p_source = b.bookmaker_name       as is_own_probability,
    e.ev,
    {{ platform_time('e.payload_fire_time') }} as payload_fire_time,
    {{ platform_time('e.probability_fire_time') }} as probability_fire_time,
    e.probability_spread_seconds,
    e.probability_spread_seconds <= {{ var('max_leg_spread_seconds') }} as is_fresh,
    {{ platform_time('e.detected_at') }} as detected_at,
    e.consumed_at

from {{ source('core', 'fact_ev_signal') }} e
join {{ source('core', 'dim_bookmaker') }} b using (bookmaker_id)
