"""Print what the runner is doing, from the status file it writes every 30 s.

Run:
    python -m runner.status            # on the host
    docker exec arbibet-runner python -m runner.status
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arbibet_capstone.warehouse import database_path  # noqa: E402


def _age(iso: str | None) -> str:
    if not iso:
        return "-"
    then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    seconds = (datetime.now(UTC) - then).total_seconds()
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def main() -> int:
    path = database_path().parent / "status.json"
    if not path.exists():
        print(f"no status file at {path}: is the runner running?")
        return 1
    status = json.loads(path.read_text(encoding="utf-8"))
    print(f"status written {_age(status['written_at'])}\n")
    print("COMPONENTS")
    for beat in status["heartbeats"]:
        detail = beat.get("detail") or {}
        if isinstance(detail, str):
            detail = json.loads(detail)
        summary = ", ".join(f"{k}={v}" for k, v in list(detail.items())[:6])
        print(f"  {beat['component']:9} {beat['state']:10} {_age(beat['beat_at']):>9}  {summary}")
    print("\nRECENT RUNS")
    for run in status["runs"][:25]:
        took = ""
        if run.get("finished_at"):
            start = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
            took = f"{(end - start).total_seconds():.0f}s"
        error = f"  {run['error'][:80]}" if run.get("error") else ""
        print(f"  {_age(run['started_at']):>9} {run['job']:28} {run['status']:8} {took:>6}{error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
