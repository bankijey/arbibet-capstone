-- A rate outside 0..1 means the pooling rule broke.
--
-- The first version of gold_slip_leg_history averaged both teams' records for
-- every family, which put Bayern Munich's 9-in-10 win rate behind an Osnabruck
-- home win at 38.89 and called it "0.700 historical". That produced a number
-- inside 0..1, so this test would not have caught it -- but it catches the
-- next mistake of that shape, where wins and matches come from different
-- groupings and the ratio escapes the range.

select
    share_code,
    leg_index,
    historical_rate
from {{ ref('gold_slip_leg_history') }}
where historical_rate is not null
  and (historical_rate < 0 or historical_rate > 1)
