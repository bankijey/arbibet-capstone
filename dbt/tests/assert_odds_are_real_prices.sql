-- Decimal odds below 1.01 are not prices.
--
-- 1.00 returns the stake and nothing else; 0.00 returns nothing at all. No
-- book offers either as a bet -- they are how a book says "this outcome is not
-- currently available". One of the five emits 0.00 for a suspended outcome
-- rather than omitting it, and 42 of the first 7,765 ticks extracted were
-- exactly that.
--
-- Left in, they draw a price collapsing to zero and recovering, which reads as
-- a violent market move rather than a suspension. This test exists because the
-- only thing that caught it the first time was an axis on a chart reaching
-- zero when it should not have.

select event_id, market_id, outcome_id, bookmaker_id, odds, fire_time
from {{ source('core', 'fact_odds_tick') }}
where odds <= 1.0
