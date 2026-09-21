-- "Does this slip make sense?", answered per leg with evidence.
--
-- For every leg of every booking slip, how often each side has actually landed
-- that exact market recently. The leg says "Over 2.5"; this says the two teams
-- have gone over in 7 of their last 10, and the book is offering 1.82.
--
-- This is the join the whole project was building toward, and it needs no new
-- source. The slip's market is a betradar id; the settled history is keyed on
-- the same taxonomy; the fixture resolves to API-Football team ids through the
-- matcher. Three tables already loaded.
--
-- WHAT THIS IS NOT: a probability. `historical_rate` is a base rate over a
-- team's last ten matches, and it knows nothing about who they played.
-- Osnabruck have won 5 of 10 and are 38.89 to beat Bayern Munich; the honest
-- reading is "0.500 against their usual opposition", not "50% here". Ten
-- matches is also a small sample -- one result moves it by ten points.
--
-- It is still the right number to show, because it is what the punter can
-- check, and the gap between it and `implied_rate` is where a conversation
-- starts. Anything consuming this -- the LLM summariser above all -- must
-- present it as recent form, never as an estimate of this fixture.
--
-- Only matches BEFORE the fixture count. Using a team's later results to judge
-- a slip offered today is the most inviting mistake available here, and the
-- `match_date < kickoff_at` predicate is the only thing preventing it.

{% set form_window = 10 %}

with leg as (
    select *
    from {{ ref('stg_slip_leg') }}
    where side_or_line is not null
      and event_id is not null
),

-- Each leg asks the question twice, once per side of the fixture.
leg_team as (
    select share_code, leg_index, kickoff_at, 'home' as team_role, home_team_id as team_id from leg
    union all
    select share_code, leg_index, kickoff_at, 'away' as team_role, away_team_id as team_id from leg
),

ranked as (
    select
        lt.share_code,
        lt.leg_index,
        lt.team_role,
        r.verdict,
        row_number() over (
            partition by lt.share_code, lt.leg_index, lt.team_role
            order by r.match_date desc
        ) as recency
    from leg_team lt
    join leg l
      on l.share_code = lt.share_code and l.leg_index = lt.leg_index
    join {{ source('core', 'fact_team_market_result') }} r
      on  r.team_id       = lt.team_id
      and r.market_family = l.market_family
      and r.period        = l.period
      and r.side_or_line  = l.side_or_line
      and r.match_date    < lt.kickoff_at
),

form as (
    select
        share_code,
        leg_index,
        team_role,
        count(*)                                        as matches,
        sum(case when verdict = 'won' then 1 else 0 end) as wins
    from ranked
    where recency <= {{ form_window }}
    group by share_code, leg_index, team_role
)

select
    l.share_code,
    l.followed_times,
    l.leg_index,
    l.event_id,   -- for linking the leg to its match on the book
    l.home_team,
    l.away_team,
    l.tournament,
    l.kickoff_at,
    l.market_name,
    l.market_family,
    l.period,
    l.side_or_line,
    l.outcome_name,
    l.odds,
    l.book_probability,

    h.matches                                           as home_matches,
    h.wins                                              as home_wins,
    a.matches                                           as away_matches,
    a.wins                                              as away_wins,

    -- The recent hit rate for THIS leg, and the reason it is not simply the
    -- average of the two sides.
    --
    -- After team_perspective mirrors, a team's row for `side` says whether
    -- that side resolved in ITS favour. So pooling the two teams is valid only
    -- for a SYMMETRIC family, where both observe the same fact: "over 2.5"
    -- happened, or it did not, and both sides agree.
    --
    -- For a DIRECTIONAL family the two rows are different claims. Averaging
    -- them produced this, on a real slip: Osnabruck to beat Bayern Munich at
    -- 38.89, "historical rate 0.700" -- because Bayern win 9 of 10, and their
    -- record was folded in as though it supported an Osnabruck win. It is
    -- evidence against the leg. Only the side the leg names counts.
    case
        when l.classification = 'symmetric'
             and coalesce(h.matches, 0) + coalesce(a.matches, 0) > 0
            then (coalesce(h.wins, 0) + coalesce(a.wins, 0))
                 / (coalesce(h.matches, 0) + coalesce(a.matches, 0))
        when l.classification = 'symmetric' then null
        when l.side_or_line like 'away%' and a.matches > 0 then a.wins / a.matches
        when l.side_or_line like 'away%' then null
        when h.matches > 0 then h.wins / h.matches
        else null
    end                                                 as historical_rate,

    -- The counts BEHIND that rate, from the same branch. Selecting the home
    -- side's numbers next to an away side's rate handed the summariser
    -- "90%" beside "6/10" and it wrote a sentence contradicting itself. A
    -- rate and its numerator must never be able to come from different
    -- places, which is why this is one CASE and not two columns picked by a
    -- caller.
    case
        when l.classification = 'symmetric' then coalesce(h.wins, 0) + coalesce(a.wins, 0)
        when l.side_or_line like 'away%' then a.wins
        else h.wins
    end                                                 as history_wins,
    case
        when l.classification = 'symmetric' then coalesce(h.matches, 0) + coalesce(a.matches, 0)
        when l.side_or_line like 'away%' then a.matches
        else h.matches
    end                                                 as history_matches,

    l.classification,

    -- What the book's price implies, before its margin is removed. Comparing
    -- the two is the leg-level version of the whole platform's question.
    1 / nullif(l.odds, 0)                               as implied_rate,

    -- How the leg actually settled: won | lost | push | void | half_win |
    -- half_loss | unsettleable, NULL until the match is settled. Read from the
    -- PRIMARY team's row, which carries the fixture-level verdict; the other
    -- team's row is mirrored, and reading it said "away won" about a 0-0 draw
    -- (FINDINGS 13c). `dim_market_outcome.primary_team` says which row.
    -- ...or, until API-Football has it, pass 1's verdict from the book's own
    -- final score (odds/settle_fast.py).
    -- Once pass 2 has spoken (confirmed | corrected) its verdict is the one.
    case when p.stage in ('confirmed', 'corrected') then p.verdict
         else coalesce(v.verdict, p.verdict) end        as resolution

from leg l
left join {{ source('core', 'fact_team_market_result') }} v
       on v.fixture_id    = l.apifootball_id
      and v.team_id       = if(l.primary_team = 'away', l.away_team_id, l.home_team_id)
      and v.market_family = l.market_family
      and v.period        = l.period
      and v.side_or_line  = l.side_or_line
left join {{ source('core', 'fact_outcome_result') }} p
       on p.event_id      = l.event_id
      and p.market_family = l.market_family
      and p.period        = l.period
      and p.side_or_line  = l.side_or_line
left join form h on h.share_code = l.share_code and h.leg_index = l.leg_index and h.team_role = 'home'
left join form a on a.share_code = l.share_code and a.leg_index = l.leg_index and a.team_role = 'away'
