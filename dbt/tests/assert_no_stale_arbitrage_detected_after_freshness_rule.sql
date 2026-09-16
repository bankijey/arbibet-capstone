-- The plausibility test now ignores stale-leg signals, so something has to
-- guard that the ignored set is HISTORY and not still growing. The detector
-- refuses any arbitrage whose legs span more than `max_leg_spread_seconds`;
-- a stale row consumed after the rule shipped means a consumer is running old
-- code, or someone widened ARB_MAX_LEG_SPREAD_SECONDS without saying so.
--
-- Keyed on consumed_at, not detected_at: detected_at is market time, and an
-- old snapshot replayed today is exactly the case this must catch.

select signal_key, leg_spread_seconds, consumed_at
from {{ ref('stg_arbitrage_signal') }}
where not is_fresh
  and consumed_at > '2026-09-15 11:30:00 +00:00'::timestamp_tz
