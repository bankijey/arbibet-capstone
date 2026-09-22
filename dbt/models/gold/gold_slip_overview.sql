-- One row per booking slip: what a slip card needs before its legs are opened.
-- Lets the dashboard pick which slips to show without fetching every leg of
-- every slip (23,000 rows) on each cold load.

select
    share_code,
    count(*)                                         as legs,
    min(kickoff_at)                                  as first_kickoff,
    max(kickoff_at)                                  as last_kickoff,
    sum(if(resolution in ('won', 'half_win'), 1, 0))   as won,
    sum(if(resolution in ('lost', 'half_loss'), 1, 0)) as lost,
    -- Product of every leg's price: exp(sum(ln)), no product aggregate exists.
    exp(sum(if(odds > 0, ln(odds), 0)))             as combined_odds,
    -- The part of the accumulator already secured: the product of the legs that
    -- have won. A slip with two legs won at 1.8 and 2.1 has 3.78 locked in and
    -- needs only what is left to land; ranks live slips on the slips page.
    exp(sum(if(odds > 0 and resolution in ('won', 'half_win'), ln(odds), 0))) as locked_in,
    -- ...and what still has to land: the product of the legs without a result.
    exp(sum(if(odds > 0 and resolution is null, ln(odds), 0)))             as pending_odds
from {{ ref('gold_slip_leg_history') }}
group by share_code
