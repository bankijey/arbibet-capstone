-- The check that caught three crosswalk defects, kept as a standing test.
--
-- 174 apparent arbitrage opportunities were once flagged, the worst at 7.83x
-- -- betting every outcome for 783% of stake. Every one was a parser bug: a
-- collapsed over/under ladder, an unnormalised asian handicap, a suspended
-- market read as a live price. None raised an error; each produced a
-- plausible-looking number.
--
-- Anything outside this range is a mapping defect until proven otherwise. It
-- is deliberately wide: a real 4% edge on an obscure market is believable, a
-- 30% one on a mainstream market at five books is not.

--
-- Scoped to FRESH signals. Stale-leg sets were the 48 failures this test raised
-- on every run from 2026-09-07: real prices, never on sale together. That is a
-- known, measured cause with its own rule -- refused at detection, excluded
-- from `is_surebet` -- not an unexplained mapping defect, which is what this
-- test exists to catch. Inside the freshness bar the range still holds and
-- still means what it always did.

select
    signal_key,
    market_id,
    arbitrage,
    leg_spread_seconds
from {{ ref('stg_arbitrage_signal') }}
where is_fresh
  and (arbitrage < 0.5 or arbitrage > 1.10)
