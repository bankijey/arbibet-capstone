-- Arbitrage signals, with the two facts a reader has to know made explicit.
--
-- 1. Most rows are NOT surebets. ARB_RECORD_THRESHOLD defaults to 0.98, so the
--    table records near-arbitrage -- the best-price shortfall across books --
--    because genuine surebets across public bookmakers barely exist.
--    `is_surebet` is the distinction, and `overround` is the honest metric:
--    how far short of a guaranteed return the best available prices fall.
--
-- 2. Legs can be hours apart. A snapshot holds each book's LAST SEEN price, so
--    a book that stopped publishing contributes a dead one. Observed spreads
--    reach 108,927 seconds. `is_fresh` marks sets inside the freshness bar,
--    and `is_surebet` REQUIRES it: every one of the 48 implausible arbitrages
--    ever recorded had legs more than five minutes apart, none inside it
--    (FINDINGS 13g). A stale set is not a weaker surebet, it is not one.
--    The rows stay, flagged, as the evidence for that rule. The detector
--    refuses new ones, so these are history.

select
    signal_key,
    event_id,
    market_id,
    market_base_id,
    specifier,
    arbitrage,
    arbitrage > 1
      and leg_spread_seconds <= {{ var('max_leg_spread_seconds') }}
                                        as is_surebet,
    (1 / arbitrage) - 1                 as overround,
    n_legs,
    legs,
    leg_spread_seconds,
    leg_spread_seconds <= {{ var('max_leg_spread_seconds') }}
                                        as is_fresh,
    newest_leg_fire_time,
    detected_at,                        -- market time (= newest_leg_fire_time)
    consumed_at                         -- when a consumer processed it

from {{ source('core', 'fact_arbitrage_signal') }} s
-- A signal with a leg from a book found pricing a different match under this
-- event id is not a signal (arbibet_capstone.verify; core.fixture_check).
where not exists (
    select 1
    from unnest(cast(s.legs as json[])) as leg(value)
    join {{ source('core', 'fixture_check') }} c
      on c.event_id = s.event_id
     and c.bookmaker_name = leg.value ->> '$.bookmaker'
     and c.verdict = 'mismatch'
)
