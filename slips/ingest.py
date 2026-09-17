"""Fetch msport booking codes into `bronze_slip_payload`.

Fetch, hash, merge. No parsing -- the payload is stored verbatim and every
question about it is answered by a dbt model downstream. That split exists
because slips have no history endpoint: a parser bug here would be permanent
data loss, and three parser defects turned up in this project in a single day.

Run:
    python slips/ingest.py
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from arbibet_capstone.env import load as load_env
from arbibet_capstone.slips import SOURCE, Settings, fetch_slips, open_client, payload_hash
from arbibet_capstone.warehouse import append_slips, connect

load_env()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("slips.ingest")


# Each slip's NEWEST stored payload, and how many had copied it then.
#
# A fetched slip is skipped only when it matches THAT row exactly -- same
# content, same copy count. Every run used to MERGE all 200 regardless, which
# at half-hourly is 9,600 rewrites a day of rows that had not changed, each one
# also moving the warehouse watermark the dashboard refreshes on.
#
# Compared against the newest row, not "any row with this hash": a slip edited
# from A to B and back to A must still write, or the dashboard's
# newest-payload-per-slip would keep showing B. And a changed copy count
# writes too -- it is what ranks "popular".
_NEWEST = """
    SELECT source, share_code, payload_hash, followed_times
    FROM CORE.bronze_slip_payload
    QUALIFY row_number() OVER (
        PARTITION BY source, share_code ORDER BY last_fetched_at DESC
    ) = 1
"""


def main() -> int:
    settings = Settings.from_env()
    log.info(
        "fetch budget: %d pages, %.1fs between requests",
        settings.max_pages,
        settings.delay_seconds,
    )

    fetched_at = datetime.now(UTC)
    rows = []
    with open_client() as client:
        for ref, payload in fetch_slips(client, settings):
            rows.append(
                {
                    "source": SOURCE,
                    "share_code": ref.share_code,
                    "payload_hash": payload_hash(payload),
                    "payload": payload,
                    "list_id": ref.list_id,
                    "followed_times": ref.followed_times,
                    "folds": ref.folds,
                    # first_fetched_at is deliberately absent: it defaults on
                    # insert and the MERGE never touches a column it was not
                    # given, so it keeps the moment this content first appeared.
                    "last_fetched_at": fetched_at,
                }
            )

    if not rows:
        log.warning("fetch returned nothing")
        return 1

    with connect() as warehouse:
        with warehouse.cursor() as cur:
            cur.execute(_NEWEST)
            newest = {
                (source, code): (digest, followed)
                for source, code, digest, followed in cur.fetchall()
            }
        changed = [
            r
            for r in rows
            if newest.get((r["source"], r["share_code"]))
            != (r["payload_hash"], r["followed_times"])
        ]
        # Appended to the Parquet lake, not merged: see warehouse.append_slips.
        written = append_slips(warehouse, changed) if changed else 0
    log.info(
        "slips fetched=%d unchanged=%d merged=%d",
        len(rows),
        len(rows) - len(changed),
        written,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
