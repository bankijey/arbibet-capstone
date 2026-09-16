"""Remember how far a job got, so the next run does not redo it.

`odds/ticks.py` proved the shape: replaying everything every run cost 9,732
payloads and 48 minutes to derive rows that were already in the table. A
per-key position turned that into 1,150 payloads and 2 minutes. This is the
same idea, made reusable and persisted, so the producer can skip a fixture
whose prices have not moved since it last looked.

`scope` namespaces the keys, because two jobs tracking the same fixture are
tracking different things: the producer's position is "the newest bronze write
I have published", the tick extractor's is "the newest price I have stored".
One table, no collisions.

**A cursor is an optimisation and must never become a correctness dependency.**
Every reader here degrades to "no position known", which makes the caller do
the full amount of work -- slower, never wrong. That is why `read` swallows its
errors: a warehouse that is down should make the producer slow, not broken.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

TABLE = "pipeline_cursor"

_READ = f"SELECT key, position FROM CORE.{TABLE} WHERE scope = %s"


def read(conn: Any, scope: str) -> dict[str, datetime]:
    """Positions recorded for `scope`, or `{}` if none can be read.

    Failure is deliberately not raised. An empty mapping means "nothing is
    known", which every caller already handles by doing the whole job.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(_READ, (scope,))
            return {str(k): p for k, p in cur.fetchall()}
    except Exception:
        log.warning("cursor read failed for scope=%s; processing everything", scope, exc_info=True)
        return {}


def write(conn: Any, scope: str, positions: Mapping[str, datetime]) -> int:
    """Record `positions` for `scope`. MERGE, so re-running converges.

    Imported here rather than at module scope: `warehouse` imports the
    Snowflake connector, and this module is also read by callers that only
    ever want `read`.
    """
    if not positions:
        return 0
    from arbibet_capstone.warehouse import merge_bulk

    rows = [
        {"scope": scope, "key": key, "position": position}
        for key, position in positions.items()
    ]
    return merge_bulk(conn, table=TABLE, rows=rows, key=["scope", "key"])
