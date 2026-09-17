-- A TABLE, unlike the other staging models: every slip payload's JSON is
-- parsed here (~2 GB of text once decompressed), and as a view both gold slip
-- models paid that parse again, about 30 seconds each.
{{ config(materialized='table') }}

-- One row per leg of every booking slip, in canonical market terms.
--
-- Slips are stored verbatim as JSON, so this is where they become
-- queryable. Two id spaces have to be bridged, and neither is obvious:
--
--   * A slip names its fixture `sr:match:73936890` and its teams
--     `sr:competitor:5981`. The competitor id appears NOWHERE else in this
--     warehouse -- the history facts are keyed on API-Football team ids -- so
--     the only usable route is sr:match -> dim_fixture -> the API-Football
--     team ids the matcher resolved.
--   * A slip's `market.id` and `outcome.id` ARE betradar ids, the same
--     namespace the settlement engine speaks, so dim_market_outcome resolves
--     them directly.
--
-- The joins are LEFT: a leg whose fixture the matcher never saw, or whose
-- market the map does not cover, is still a leg the punter backed. Dropping it
-- would quietly shrink every slip and make the soundness maths wrong.

-- DEDUPLICATION, in two places, both of which were producing duplicate legs.
--
-- 1. `bronze_slip_payload` is keyed (source, share_code, payload_hash), so the
--    SAME slip fetched twice with different content -- which is normal, legs
--    vanish at kickoff -- is stored as TWO rows, not an update. Flattening both
--    gave every re-fetched slip its legs twice: 598 payload rows for 409
--    slips, and 2,247 duplicated (share_code, leg_index) pairs downstream.
--    The newest payload is the slip as it now stands, so that is the one.
--
-- 2. `dim_fixture` is keyed on `event_id`, NOT on `sr_match_id`, and 12
--    sr_match_ids resolve to more than one fixture row -- the matcher can
--    produce several events for one betradar match. Joining on it fans every
--    leg out once per matching row.
--
-- Both are the same mistake: reading an append-or-multi-keyed table as though
-- it were current-state-by-the-key-you-happen-to-be-joining-on. The same shape
-- appears in `gold_slip_summary_ai` (see the dashboard's QUALIFY).
with payload as (
    -- Newest version per slip, already selected by the lake view.
    select *
    from {{ source('core', 'bronze_slip_payload_latest') }}
),

fixture as (
    select *
    from {{ source('core', 'dim_fixture') }}
    where sr_match_id is not null
    -- Prefer the row that actually resolved team ids: without them the leg
    -- reaches no settled history, so a fixture row that has them is strictly
    -- more useful than one that does not. `loaded_at` breaks the remaining tie.
    qualify row_number() over (
        partition by sr_match_id
        order by if(home_team_id is null, 1, 0), loaded_at desc
    ) = 1
),

legs as (
    select
        s.share_code,
        s.followed_times,
        s.last_fetched_at,
        -- 0-based, as Snowflake's FLATTEN index was; ORDINALITY counts from 1.
        l.idx - 1                                   as leg_index,
        l.value ->> '$.event.eventId'               as sr_match_id,
        l.value ->> '$.event.homeTeam'              as home_team,
        l.value ->> '$.event.awayTeam'              as away_team,
        l.value ->> '$.event.tournament'            as tournament,
        -- Epoch milliseconds have no offset of their own; to_timestamp gives
        -- an instant, rendered in session time (Europe/Berlin).
        to_timestamp((l.value ->> '$.event.startTime')::bigint / 1000) as kickoff_at,
        l.value ->> '$.market.id'                   as market_id,
        l.value ->> '$.market.specifiers'           as specifiers,
        l.value ->> '$.outcome.id'                  as outcome_id,
        l.value ->> '$.outcome.description'         as outcome_name,
        (l.value ->> '$.outcome.odds')::double      as odds,
        -- msport's own model probability. It embeds their margin, and it is
        -- what the punter was shown, which makes it the right prior to judge
        -- the slip by even though it is not truth.
        (l.value ->> '$.outcome.probability')::double as book_probability
    from payload s,
         unnest(cast(s.payload -> '$.bettableBetSlip' as json[])) with ordinality as l(value, idx)
)

select
    legs.*,
    o.market_family,
    o.period,
    o.classification,
    o.market_name,
    -- side_or_line in the settlement engine's format: the side alone, or
    -- side@line where the family declares one. The line lives in the
    -- specifier ("total=2.5"), never in the outcome id.
    case
        when o.side is null then null
        when o.has_line then o.side || '@' || split_part(legs.specifiers, '=', 2)
        else o.side
    end                                             as side_or_line,
    f.event_id,
    f.home_team_id,
    f.away_team_id,
    f.apifootball_id,
    o.primary_team

from legs
left join {{ source('core', 'dim_market_outcome') }} o
       on o.market_id = legs.market_id
      and o.outcome_id = legs.outcome_id
left join fixture f
       on f.sr_match_id = legs.sr_match_id
