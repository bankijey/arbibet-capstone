"""The pipeline runner: one always-on process instead of Airflow.

    listener thread   LISTEN on markets bronze; wakes the hot loop instantly
    hot thread        surebets and EV within seconds of a new price
    main thread       warm cycle every 15 minutes, cold cycle daily at 06:00

Why one process: the warehouse is a DuckDB file, and DuckDB lets a single
process write to it. Every job runs here and shares one connection, so there
is nothing to contend for a lock and nothing to schedule across processes.

Observability replaces the Airflow UI: every job run lands in `ops.job_run`,
every component beats into `ops.heartbeat`, and both are mirrored to the
serving database for the dashboard's Pipeline health page. Logs go to stdout
(`docker logs`) and to data/logs/runner.log, rotated.

Run:
    python -m runner.main              # forever
    python -m runner.main --warm-once  # one warm cycle, then exit
    python -m runner.main --cold-once  # one cold cycle, then exit
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from arbibet_capstone.env import load as load_env  # noqa: E402
from arbibet_capstone.warehouse import Warehouse, connect, database_path  # noqa: E402
from runner.hot import HotLoop  # noqa: E402
from runner.jobs import Job, cold_jobs, warm_jobs  # noqa: E402
from runner.listener import BronzeListener  # noqa: E402
from runner.observe import Observer  # noqa: E402

load_env()
log = logging.getLogger("runner")

BERLIN = ZoneInfo("Europe/Berlin")
WARM_MINUTES = float(os.environ.get("WARM_MINUTES", "15"))
COLD_HOUR = int(os.environ.get("COLD_HOUR", "6"))


def _logging() -> None:
    folder = database_path().parent / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            folder / "runner.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
        ),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    for noisy in ("httpx", "openai", "py4j", "snowflake"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def run_cycle(loop: str, jobs: list[Job], observer: Observer, last_run: dict[str, datetime],
              stop: threading.Event) -> None:
    observer.beat(loop, "working")
    started = time.monotonic()
    failed: list[str] = []
    for job in jobs:
        if stop.is_set():
            break
        key = f"{loop}.{job.name}"
        now = datetime.now(UTC)
        if job.every and key in last_run and now - last_run[key] < job.every:
            continue
        observer.beat(loop, "working", job=job.name)
        try:
            with observer.run(key, loop) as run:
                log.info("%s: start", key)
                job.fn(run)
            last_run[key] = now
            log.info("%s: %s", key, "skipped" if run.skipped else "ok")
        except Exception:
            failed.append(job.name)
            log.error("%s: failed", key, exc_info=True)
    observer.beat(
        loop,
        "idle",
        last_cycle_seconds=round(time.monotonic() - started),
        last_cycle_failed=failed,
        finished_at=datetime.now(UTC),
    )


def _next_cold(now: datetime) -> datetime:
    local = now.astimezone(BERLIN)
    target = local.replace(hour=COLD_HOUR, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warm-once", action="store_true")
    parser.add_argument("--cold-once", action="store_true")
    args = parser.parse_args()

    _logging()
    warehouse: Warehouse = connect()
    observer = Observer(warehouse)
    stop = threading.Event()
    last_run: dict[str, datetime] = {}

    if args.warm_once or args.cold_once:
        loop = "warm" if args.warm_once else "cold"
        jobs = warm_jobs(warehouse) if args.warm_once else cold_jobs(warehouse, observer)
        run_cycle(loop, jobs, observer, last_run, stop)
        warehouse.raw.execute("CHECKPOINT")
        return 0

    def shutdown(signum: int, _frame: object) -> None:
        log.info("signal %s: finishing the current job, then stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    listener = BronzeListener(os.environ["MARKETS_DB_URL"], observer, stop)
    listener.start()
    hot = HotLoop(warehouse, observer, listener, stop)
    hot.start()

    observer.beat("runner", "started", pid=os.getpid(), started_at=datetime.now(UTC))
    log.info("runner started: warm every %.0f min, cold daily at %02d:00 Berlin",
             WARM_MINUTES, COLD_HOUR)
    next_warm = datetime.now(UTC)
    next_cold = _next_cold(datetime.now(UTC))
    warm, cold = warm_jobs(warehouse), cold_jobs(warehouse, observer)
    schedule = {"next_warm": next_warm, "next_cold": next_cold}
    threads = {"hot": hot}

    def supervise() -> None:
        # Its own thread, so heartbeats and the status file stay current while
        # a long warm or cold cycle holds the main thread.
        while not stop.is_set():
            observer.beat(
                "runner",
                "running",
                **schedule,
                listener_connected=listener.connected,
                hot_alive=threads["hot"].is_alive(),
            )
            observer.write_status(database_path().parent / "status.json")
            if not threads["hot"].is_alive() and not stop.is_set():
                log.error("hot loop died; restarting it")
                threads["hot"] = HotLoop(warehouse, observer, listener, stop)
                threads["hot"].start()
            stop.wait(30)

    threading.Thread(target=supervise, name="supervisor", daemon=True).start()
    while not stop.is_set():
        now = datetime.now(UTC)
        if now >= schedule["next_cold"]:
            run_cycle("cold", cold, observer, last_run, stop)
            schedule["next_cold"] = _next_cold(datetime.now(UTC))
        elif now >= schedule["next_warm"]:
            run_cycle("warm", warm, observer, last_run, stop)
            schedule["next_warm"] = datetime.now(UTC) + timedelta(minutes=WARM_MINUTES)
        stop.wait(5)

    threads["hot"].join(timeout=60)
    warehouse.raw.execute("CHECKPOINT")
    observer.beat("runner", "stopped")
    log.info("runner stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
