"""What the Airflow UI used to show, recorded by the runner itself.

Two tables in the warehouse's `ops` schema (sql/duckdb_ddl.sql):

    ops.job_run    one row per job execution: when, how long, ok or failed,
                   what it did, and the error if it failed.
    ops.heartbeat  one row per component (runner, listener, hot, warm, cold),
                   overwritten on every beat, so "is it alive and what is it
                   doing" is one SELECT.

Both are mirrored to the serving database with the rest of the dashboard data,
and read by the dashboard's Pipeline health page, so the pipeline can be
watched from anywhere rather than from a UI on this machine. `runner/status.py`
prints the same picture in a terminal.

Recording must never take the pipeline down: every write here is wrapped, and
a failure to record is logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from arbibet_capstone.warehouse import Warehouse, merge_bulk

log = logging.getLogger("runner.observe")

# job_run rows older than this are deleted by the cold loop. At ~150 warm and
# ~1,440 hot summary rows a day, a fortnight is ~25k rows: plenty to spot a
# pattern, small enough never to matter.
RETENTION = timedelta(days=14)


@dataclass
class Run:
    """Filled in by the job while it runs; written when it finishes."""

    run_id: str
    job: str
    loop: str
    trigger: str
    started_at: datetime
    rows_written: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False


class Observer:
    def __init__(self, warehouse: Warehouse) -> None:
        self.warehouse = warehouse
        self._lock = threading.Lock()

    def _write(
        self, table: str, row: dict[str, Any], key: list[str], json_columns: set[str]
    ) -> None:
        try:
            with self._lock:
                merge_bulk(
                    self.warehouse, table=table, rows=[row], key=key, json_columns=json_columns
                )
        except Exception:
            log.warning("could not record to ops.%s", table, exc_info=True)

    @contextmanager
    def run(self, job: str, loop: str, trigger: str = "schedule") -> Iterator[Run]:
        """Record one execution of `job`. Exceptions are recorded, then re-raised."""
        record = Run(str(uuid4()), job, loop, trigger, datetime.now(UTC))
        row = {
            "run_id": record.run_id,
            "job": job,
            "loop": loop,
            "trigger": trigger,
            "started_at": record.started_at,
            "finished_at": None,
            "status": "running",
            "rows_written": None,
            "detail": None,
            "error": None,
        }
        self._write("job_run", row, ["run_id"], {"detail"})
        try:
            yield record
        except Exception as err:
            row |= {
                "finished_at": datetime.now(UTC),
                "status": "failed",
                "rows_written": record.rows_written,
                "detail": record.detail or None,
                "error": "".join(traceback.format_exception_only(type(err), err)).strip()[-2000:],
            }
            self._write("job_run", row, ["run_id"], {"detail"})
            raise
        row |= {
            "finished_at": datetime.now(UTC),
            "status": "skipped" if record.skipped else "ok",
            "rows_written": record.rows_written,
            "detail": record.detail or None,
        }
        self._write("job_run", row, ["run_id"], {"detail"})

    def record(
        self,
        job: str,
        loop: str,
        *,
        trigger: str,
        started_at: datetime,
        rows_written: int,
        detail: dict[str, Any],
    ) -> None:
        """A summary row for work that is too frequent to log one row per unit."""
        self._write(
            "job_run",
            {
                "run_id": str(uuid4()),
                "job": job,
                "loop": loop,
                "trigger": trigger,
                "started_at": started_at,
                "finished_at": datetime.now(UTC),
                "status": "ok",
                "rows_written": rows_written,
                "detail": detail,
                "error": None,
            },
            ["run_id"],
            {"detail"},
        )

    def beat(self, component: str, state: str, **detail: Any) -> None:
        self._write(
            "heartbeat",
            {
                "component": component,
                "beat_at": datetime.now(UTC),
                "state": state,
                "detail": json.loads(json.dumps(detail, default=str)) if detail else None,
            },
            ["component"],
            {"detail"},
        )

    def prune(self) -> int:
        cutoff = datetime.now(UTC) - RETENTION
        with self._lock, self.warehouse.cursor() as cur:
            cur.execute("DELETE FROM ops.job_run WHERE started_at < %s", (cutoff,))
            return cur.rowcount

    def write_status(self, path: Any) -> None:
        """Heartbeats and recent runs as JSON, for `runner/status.py`.

        A second process cannot open a DuckDB file another process is writing,
        not even read-only, so a terminal status check reads this file instead.
        """
        try:
            with self._lock:
                beats = self.warehouse.query(
                    "SELECT component, beat_at, state, detail FROM ops.heartbeat ORDER BY component"
                )
                runs = self.warehouse.query(
                    "SELECT job, loop, trigger, started_at, finished_at, status, rows_written, "
                    "detail, error FROM ops.job_run ORDER BY started_at DESC LIMIT 60"
                )
            beats.columns = [c.lower() for c in beats.columns]
            runs.columns = [c.lower() for c in runs.columns]
            payload = {
                "written_at": datetime.now(UTC).isoformat(),
                "heartbeats": json.loads(beats.to_json(orient="records", date_format="iso")),
                "runs": json.loads(runs.to_json(orient="records", date_format="iso")),
            }
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=1, default=str)
            os.replace(tmp, path)
        except Exception:
            log.warning("could not write status file", exc_info=True)
