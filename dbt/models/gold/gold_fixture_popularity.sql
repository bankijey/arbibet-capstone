-- How many distinct booking slips name each fixture, and how often those slips
-- were copied. Ranks the dashboard's deep dives.
--
-- A table rebuilt each run rather than a query on page load: ranking needs every
-- slip's newest payload flattened, and doing that per visit made the slips page
-- take 2.5s on this query alone after every pipeline write.

select
    l.event_id,
    count(distinct l.share_code)  as slips,
    sum(l.followed_times)         as follows
from {{ ref('stg_slip_leg') }} l
where l.event_id is not null
group by l.event_id
