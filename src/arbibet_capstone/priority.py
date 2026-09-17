"""Let the hot loop go first.

The runner is one process: the hot loop (a new price -> a stored signal, in
seconds) shares the CPU, the GIL and the DuckDB connection with warm jobs that
parse thousands of payloads. Measured before this existed, hot-loop latency
rose from 2.5-5 s idle to a median of 7-140 s while warm jobs ran.

So the hot loop marks itself busy while it recomputes, and long warm jobs call
`yield_to_hot()` between units of work (a fixture, a batch). When the hot loop
is busy they wait -- briefly, and never for long: a warm job must still finish.
Outside the runner (a script run on its own) nothing is ever busy, so the call
costs one Event check.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

_BUSY = threading.Event()
_waited = {"seconds": 0.0, "times": 0, "last_end": 0.0}

# The longest one call waits. A recompute takes well under this; a hot loop
# stuck for longer must not stall the warm cycle behind it.
MAX_WAIT_SECONDS = 5.0
# After yielding, work at least this long before yielding again. On a busy
# afternoon the hot loop is woken every second or two, and a warm job that gave
# way every time would never finish.
MIN_WORK_SECONDS = 1.0


@contextmanager
def hot_work() -> Iterator[None]:
    """Mark the hot loop busy for the duration of the block."""
    _BUSY.set()
    try:
        yield
    finally:
        _BUSY.clear()


def hot_busy() -> bool:
    return _BUSY.is_set()


def yield_to_hot(max_wait: float = MAX_WAIT_SECONDS) -> float:
    """Wait while the hot loop is busy, up to `max_wait` seconds. Returns seconds waited."""
    if not _BUSY.is_set():
        return 0.0
    started = time.monotonic()
    if started - _waited["last_end"] < MIN_WORK_SECONDS:
        return 0.0
    deadline = started + max_wait
    while _BUSY.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)
    _waited["last_end"] = time.monotonic()
    waited = _waited["last_end"] - started
    _waited["seconds"] += waited
    _waited["times"] += 1
    return waited


def waited() -> dict[str, float]:
    """Totals since the last call, for the job that yielded to report."""
    out = {"yield_seconds": round(_waited["seconds"], 1), "yields": int(_waited["times"])}
    _waited["seconds"], _waited["times"] = 0.0, 0
    return out
