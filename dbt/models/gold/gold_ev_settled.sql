-- Every positive-EV detection on a match that has been played, with how the
-- outcome actually resolved. The input to the dashboard's backtest: "had every
-- one of these been bet, sized and timed this way, what would have happened?"
--
-- Resolution is read from the PRIMARY team's row of fact_team_market_result,
-- which carries the fixture-level verdict (the other team's row is mirrored --
-- FINDINGS 13c; the same join resolves slip legs, checked against 4,740 final
-- scores). NULL verdict = not settled yet: the match stats for a fixture arrive
-- with the daily ingestion, so a detection from last night settles tomorrow.
--
-- Grain: one row per detection. Several detections of one opportunity
-- (event, market, outcome) at different times, prices or books are kept, because
-- WHEN to take the price is one of the strategies the backtest compares.

with ev as (
    select s.*, f.kickoff_at, f.apifootball_id, f.home_team_id, f.away_team_id,
           f.home_team || ' v ' || f.away_team as fixture, f.tournament
    from {{ ref('stg_ev_signal') }} s
    join {{ source('core', 'dim_fixture') }} f on f.event_id = s.event_id
    where s.detected_at < f.kickoff_at
      and f.kickoff_at < current_timestamp()
)

select
    ev.signal_key,
    ev.event_id,
    ev.market_id,
    ev.outcome_id,
    ev.fixture,
    ev.tournament,
    coalesce(o.market_name, 'market ' || ev.market_base_id) as market_name,
    ev.specifier                                              as line,
    ev.outcome_name,
    ev.bookmaker_name,
    ev.odds,
    ev.implied_p,
    ev.ev,
    ev.is_fresh,
    ev.detected_at,
    {{ platform_time('ev.kickoff_at') }}                      as kickoff_at,
    r.verdict
from ev
left join {{ source('core', 'dim_market_outcome') }} o
       on o.market_id  = ev.market_base_id::string
      and o.outcome_id = ev.outcome_id
left join {{ source('core', 'fact_team_market_result') }} r
       on r.fixture_id    = ev.apifootball_id
      and r.team_id       = iff(o.primary_team = 'away', ev.away_team_id, ev.home_team_id)
      and r.market_family = o.market_family
      and r.period        = o.period
      and r.side_or_line  = iff(o.has_line, o.side || '@' || ev.specifier, o.side)
