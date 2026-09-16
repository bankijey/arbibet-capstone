-- A slip has one leg per index. Anything else is a fan-out.
--
-- Two independent causes were found here, and both were the same mistake:
-- reading a table as current-state when its key says otherwise.
--
--   * bronze_slip_payload is keyed (source, share_code, payload_hash). A slip
--     re-fetched after losing a leg is stored AGAIN, not updated -- 598 rows
--     for 409 slips.
--   * dim_fixture is keyed on event_id, not sr_match_id, and 12 sr_match_ids
--     resolve to more than one row.
--
-- Together they produced 2,247 duplicated (share_code, leg_index) pairs, which
-- reached the dashboard as slips listing the same leg twice and, worse, fed
-- the LLM a leg-set with repeats in it.

select share_code, leg_index, count(*) as rows_for_one_leg
from {{ ref('stg_slip_leg') }}
group by share_code, leg_index
having count(*) > 1
