"""Load the warehouse dimensions: `dim_market` and `dim_fixture`.

Both are MERGEd, not inserted, so this is safe to re-run -- which it needs to
be: the fixture list moves every day and the crosswalk changes whenever a
market is added.

Run:
    python snowflake/load_dims.py [days_back] [days_ahead]
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from arbibet_capstone import fixtures
from arbibet_capstone.dims import fixture_rows, market_outcome_rows, market_rows
from arbibet_capstone.env import load as load_env
from arbibet_capstone.warehouse import bookmaker_ids, connect, merge_bulk

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("load_dims")


# Recover team ids the rolling source can no longer supply.
#
# `apifootball_events` is a window: a fixture whose ids have aged out of it
# still matches, and still yields a row, but with NULL team ids. The MERGE now
# refuses to write those NULLs over what is already there (see `keep=` below),
# which stops the loss going forward but does not undo it -- 3,226 fixtures had
# already been blanked, and with them every slip leg's settled history.
#
# The ids are still recoverable, because two things outlive the window: the
# matcher keeps `apifootball;<id>` in `bookmaker_event_ids`, and
# `fact_team_match` holds 149,120 fixtures loaded from the ingestor's own
# bronze. Joining them on `fixture_id` reconstructs what the projection forgot.
#
# It only ever fills a NULL. This must not become a second way for the
# warehouse to overwrite a resolved id.
_BACKFILL_TEAM_IDS = """
UPDATE CORE.dim_fixture d
SET home_team_id = COALESCE(d.home_team_id, m.home_team_id),
    away_team_id = COALESCE(d.away_team_id, m.away_team_id)
FROM (
    SELECT fixture_id,
           MAX(CASE WHEN is_home THEN team_id END)     AS home_team_id,
           MAX(CASE WHEN is_home THEN opponent_id END) AS away_team_id
    FROM CORE.fact_team_match
    GROUP BY fixture_id
) m
WHERE m.fixture_id = d.apifootball_id
  AND (d.home_team_id IS NULL OR d.away_team_id IS NULL)
"""


_SR_MATCH = re.compile(r"sr:match:(\d+)")


def _resolve_links(
    links: list[fixtures.EventLink],
    window: list[fixtures.Fixture],
    books: dict[str, int],
) -> list[dict[str, Any]]:
    """One link per (fixture, book), or none when the right one is unknowable.

    About 0.5% of matched fixtures carry TWO events from the same book -- 450 of
    95,021 pairs over 60 days, e.g. sportybet sr:match:69133606 and 72852060
    for one fixture. Those are different matches, and a link to the wrong one
    is worse than no link. Where the URL carries an sr:match id (sportybet,
    msport, ilotbet) the one equal to the fixture's own sr_match_id wins. Where
    it does not (bet9ja, livescorebet), or nothing matches, the pair gets no
    link at all.
    """
    sr_of = {str(f.event_id): f.sr_match_id for f in window}
    candidates: dict[tuple[str, str], set[str]] = {}
    for link in links:
        if link.bookmaker in books:
            candidates.setdefault((str(link.event_id), link.bookmaker), set()).add(link.url)

    rows: list[dict[str, Any]] = []
    ambiguous = 0
    for (event_id, book), urls in candidates.items():
        if len(urls) > 1:
            own = (sr_of.get(event_id) or "").removeprefix("sr:match:")
            urls = {u for u in urls if (m := _SR_MATCH.search(u)) and m.group(1) == own}
        if len(urls) != 1:
            ambiguous += 1
            continue
        rows.append({"event_id": event_id, "bookmaker_id": books[book], "url": urls.pop()})
    log.info("links resolved=%d dropped as ambiguous=%d", len(rows), ambiguous)
    return rows


def main(days_back: float, days_ahead: float) -> None:
    now = datetime.now(UTC)
    with fixtures.connect() as sources:
        window = fixtures.upcoming(
            sources,
            since=now - timedelta(days=days_back),
            until=now + timedelta(days=days_ahead),
        )
        links = fixtures.event_links(
            sources,
            since=now - timedelta(days=days_back),
            until=now + timedelta(days=days_ahead),
        )

    markets = market_rows()
    fixture_records = fixture_rows(window)

    with connect() as warehouse:
        n_markets = merge_bulk(warehouse, table="dim_market", rows=markets, key=["market_base_id"])
        # Everything the matcher enriches a fixture WITH, rather than the
        # fixture's own facts. `apifootball_events` is a rolling window, so a
        # fixture that resolved last week resolves to NULL today -- and a
        # plain overwrite would erase the team ids that the entire slip x
        # settlement join depends on. Kickoff time is deliberately absent:
        # that one genuinely changes, and a postponement must be able to win.
        n_fixtures = merge_bulk(
            warehouse,
            table="dim_fixture",
            rows=fixture_records,
            key=["event_id"],
            keep=[
                "home_team_id",
                "away_team_id",
                "apifootball_id",
                "sr_match_id",
                "home_team",
                "away_team",
                "tournament",
            ],
        )
        n_outcomes = merge_bulk(
            warehouse,
            table="dim_market_outcome",
            rows=market_outcome_rows(),
            key=["market_id", "outcome_id"],
        )

        # Links rarely change once a fixture is listed, and this runs every half
        # hour over a fortnight of fixtures -- roughly five links each. Writing
        # all of them every run would be thousands of identical rewrites, so
        # only new or changed links are merged.
        books = bookmaker_ids(warehouse)
        with warehouse.cursor() as cur:
            cur.execute("SELECT event_id, bookmaker_id, url FROM CORE.dim_event_link")
            stored = {(e, int(b)): u for e, b, u in cur.fetchall()}
        link_rows = _resolve_links(links, window, books)
        changed_links = [
            r for r in link_rows if stored.get((r["event_id"], r["bookmaker_id"])) != r["url"]
        ]
        n_links = (
            merge_bulk(
                warehouse,
                table="dim_event_link",
                rows=changed_links,
                key=["event_id", "bookmaker_id"],
            )
            if changed_links
            else 0
        )

        # After the merge, never before: the backfill fills holes the merge
        # has just finished making or leaving.
        with warehouse.cursor() as cur:
            cur.execute(_BACKFILL_TEAM_IDS)
            n_recovered = cur.rowcount or 0

    log.info(
        "dim_market=%d dim_fixture=%d dim_market_outcome=%d team_ids_recovered=%d "
        "links=%d (written=%d)",
        n_markets,
        n_fixtures,
        n_outcomes,
        n_recovered,
        len(link_rows),
        n_links,
    )


if __name__ == "__main__":
    back = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0
    ahead = float(sys.argv[2]) if len(sys.argv) > 2 else 7.0
    main(back, ahead)
