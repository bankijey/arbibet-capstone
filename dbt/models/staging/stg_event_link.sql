-- A fixture's page on each bookmaker, keyed the way the signal and slip models
-- name books, so the dashboard joins on (event_id, bookmaker_name) directly.

select
    l.event_id,
    b.bookmaker_name,
    l.url
from {{ source('core', 'dim_event_link') }} l
join {{ source('core', 'dim_bookmaker') }} b using (bookmaker_id)
