"""The warm and cold loops: everything that is not a signal.

Jobs are the pipeline's existing scripts, run INSIDE the runner process -- by
importing them and calling `main()` -- rather than as subprocesses. That is
not a style choice: DuckDB lets one process hold the database file, so a
subprocess would fail on the lock. dbt runs in-process for the same reason.

WARM (every 15 minutes, in order):
    load_dims          fixtures, markets, event links      at most hourly
    ingest_slips       msport booking slips                at most every 30 min
    extract_ticks      price history for signalled/slipped fixtures
    track_arbitrage    where each surebet market stands now
    live_state         match status; are signal legs still offered
    dbt_run            staging and gold models
    brief_fixtures     pre-match AI briefs, when their evidence moved
    summarise_upcoming AI slip verdicts, 60 per run
    summarise_results  post-match AI notes
    publish            serving tables to Supabase (when configured)

COLD (daily at 06:00 Berlin):
    flatten, settle    match history and settled markets (Spark)
    dbt_build          models AND tests
    summarise_played   AI verdicts on played slips
    compact_slips      one Parquet file per month
    backup             a consistent copy of the database, 3 kept
    prune_ops          ops.job_run older than 14 days

A failing job is recorded and the cycle moves on: one broken step should cost
its own output, not everything after it. The next cycle retries it.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from arbibet_capstone.warehouse import Warehouse, compact_slips, database_path
from runner.observe import Observer, Run

log = logging.getLogger("runner.jobs")

ROOT = Path(__file__).resolve().parents[1]
_modules: dict[str, ModuleType] = {}


def _script(relative: str) -> ModuleType:
    """Import a pipeline script once, by path (they are not a package)."""
    if relative not in _modules:
        path = ROOT / relative
        name = "job_" + relative.replace("/", "_").removesuffix(".py")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _modules[relative] = module
    return _modules[relative]


@contextmanager
def _env(values: dict[str, str]):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def script(relative: str, *args: Any, env: dict[str, str] | None = None) -> Callable[[Run], None]:
    def job(run: Run) -> None:
        with _env(env or {}):
            code = _script(relative).main(*args)
        run.detail["exit_code"] = code
        if code not in (None, 0):
            raise RuntimeError(f"{relative} exited with {code}")

    return job


def dbt(*args: str) -> Callable[[Run], None]:
    def job(run: Run) -> None:
        from dbt.cli.main import dbtRunner

        project = str(ROOT / "dbt")
        result = dbtRunner().invoke(
            [*args, "--project-dir", project, "--profiles-dir", project, "--quiet"]
        )
        nodes = getattr(result.result, "results", None) or []
        run.detail["nodes"] = len(nodes)
        failed = [n.node.name for n in nodes if str(n.status) in ("error", "fail")]
        if failed:
            run.detail["failed"] = failed
        if not result.success:
            raise RuntimeError(f"dbt {' '.join(args)} failed: {failed or result.exception}")

    return job


def publish(warehouse: Warehouse) -> Callable[[Run], None]:
    def job(run: Run) -> None:
        from runner import serve

        if not serve.configured():
            run.skipped = True
            run.detail["reason"] = "SUPABASE_DB_URL not set"
            return
        run.rows_written = serve.publish(warehouse, run)

    return job


def compact(warehouse: Warehouse) -> Callable[[Run], None]:
    def job(run: Run) -> None:
        run.detail["files_removed"] = compact_slips(warehouse)

    return job


BACKUPS_KEPT = 3


def backup(warehouse: Warehouse) -> Callable[[Run], None]:
    """A consistent copy via COPY FROM DATABASE; copying the file mid-write would not be."""

    def job(run: Run) -> None:
        folder = database_path().parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"arbibet-{datetime.now(UTC):%Y%m%d}.duckdb"
        if target.exists():
            target.unlink()
        raw = warehouse.raw.cursor()
        try:
            raw.execute(f"ATTACH '{target.as_posix()}' AS backup")
            raw.execute("COPY FROM DATABASE arbibet TO backup")
            raw.execute("DETACH backup")
        finally:
            raw.close()
        lake = database_path().parent / "lake"
        if lake.exists():
            shutil.make_archive(str(target.with_suffix("")) + "-lake", "zip", lake)
        kept = sorted(folder.glob("arbibet-*.duckdb"))
        for old in kept[:-BACKUPS_KEPT]:
            old.unlink()
            Path(str(old.with_suffix("")) + "-lake.zip").unlink(missing_ok=True)
        run.detail["file"] = target.name
        run.detail["mb"] = round(target.stat().st_size / 1e6, 1)

    return job


def prune(observer: Observer) -> Callable[[Run], None]:
    def job(run: Run) -> None:
        run.rows_written = observer.prune()

    return job


@dataclass
class Job:
    name: str
    fn: Callable[[Run], None]
    # Minimum time between runs, for jobs slower-moving than their loop.
    every: timedelta | None = None


def warm_jobs(warehouse: Warehouse) -> list[Job]:
    return [
        Job("load_dims", script("snowflake/load_dims.py", 7.0, 7.0), every=timedelta(hours=1)),
        Job("ingest_slips", script("slips/ingest.py"), every=timedelta(minutes=30)),
        Job("extract_ticks", script("odds/ticks.py")),
        Job("track_arbitrage", script("odds/arbitrage_track.py")),
        Job("live_state", script("odds/live_state.py")),
        Job("dbt_run", dbt("run")),
        Job("brief_fixtures", script("enrich/fixture_summary.py")),
        Job(
            "summarise_upcoming",
            script(
                "enrich/summarise.py",
                env={"SUMMARISE_SCOPE": "upcoming", "SUMMARISE_LIMIT": "60"},
            ),
        ),
        Job("summarise_results", script("enrich/result_summary.py")),
        Job("publish", publish(warehouse)),
    ]


def cold_jobs(warehouse: Warehouse, observer: Observer) -> list[Job]:
    return [
        Job("flatten", script("spark/flatten.py", env={"FLATTEN_SINCE_DAYS": "3"})),
        Job("settle", script("spark/settle.py")),
        Job("dbt_build", dbt("build")),
        Job(
            "summarise_played",
            script(
                "enrich/summarise.py",
                env={"SUMMARISE_SCOPE": "played", "SUMMARISE_LIMIT": "60"},
            ),
        ),
        Job("compact_slips", compact(warehouse)),
        Job("backup", backup(warehouse)),
        Job("prune_ops", prune(observer)),
        Job("publish", publish(warehouse)),
    ]
