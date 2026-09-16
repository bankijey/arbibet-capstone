-- Where every market that ever carried a fresh surebet has stood since: one row
-- per state change, below 1.0 included, rebuilt by replaying bronze through the
-- detector (odds/arbitrage_track.py). `arbitrage` NULL means no cross-book price
-- at that moment. `is_fresh` applies the detector's own bar.

select
    event_id,
    market_id,
    {{ platform_time('observed_at') }} as observed_at,
    arbitrage,
    leg_spread_seconds,
    leg_spread_seconds <= {{ var('max_leg_spread_seconds') }} as is_fresh,
    {{ platform_time('newest_leg_fire_time') }} as newest_leg_fire_time
from {{ source('core', 'fact_arbitrage_track') }}
