"""Which fixtures get a deep-dive page.

The fixtures punters actually built slips on, ranked by how many DISTINCT slips
name them -- not by copies, so one hugely-copied slip cannot make its fixtures
look widely backed -- and using only the newest payload per slip, because a
re-fetched slip is stored again rather than replaced (FINDINGS 8b).

Ranked separately for fixtures still to be played and fixtures already
started. Slip counts accumulate over time, so a single ranking returns only old
fixtures and the upcoming tab would be empty (FINDINGS 13h).

Two pipeline steps need this list and must agree on it: `odds/ticks.py`
extracts their price history, and `enrich/fixture_summary.py` writes their
briefs. It lived as a copy in each until the brief writer's copy was missing
and every upcoming deep dive opened with no summary. The dashboard keeps its
own SQL, because it deliberately imports nothing from this package.
"""

from __future__ import annotations

# The dashboard shows ten upcoming deep dives as cards and twenty past ones as a
# list, so the tick extractor and the brief writer must cover the same thirty.
DEEP_DIVE_UPCOMING = 10
DEEP_DIVE_PAST = 20

# Returns `event_id, upcoming`: the top `%(upcoming)d` still to be played and the
# top `%(past)d` already started.
POPULAR_FIXTURES = """
    WITH leg AS (
        -- Only the fixture id of each leg, as a typed struct: casting the leg
        -- array to JSON[] parsed every field of every leg and ran the runner's
        -- DuckDB out of memory (see stg_slip_leg).
        SELECT s.share_code, l.leg.event.eventId AS sr_match_id
        FROM CORE.bronze_slip_payload_latest s,
             unnest(json_transform(
                 s.payload -> '$.bettableBetSlip', '[{"event": {"eventId": "VARCHAR"}}]'
             )) AS l(leg)
    )
    SELECT f.event_id,
           coalesce(f.kickoff_at > current_timestamp, FALSE) AS upcoming
    FROM leg
    JOIN CORE.dim_fixture f ON f.sr_match_id = leg.sr_match_id
    GROUP BY f.event_id, f.kickoff_at
    QUALIFY row_number() OVER (
        PARTITION BY coalesce(f.kickoff_at > current_timestamp, FALSE)
        ORDER BY count(DISTINCT leg.share_code) DESC
    ) <= if(coalesce(f.kickoff_at > current_timestamp, FALSE), %(upcoming)d, %(past)d)
"""


def popular_fixtures_sql() -> str:
    return POPULAR_FIXTURES % {"upcoming": DEEP_DIVE_UPCOMING, "past": DEEP_DIVE_PAST}
