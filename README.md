# arbibet-capstone

Ironhack Data Engineering capstone. Reads the append-only bronze layer of the
Arbibet markets collector, normalises five bookmakers into one canonical market
taxonomy, and publishes canonical per-fixture price snapshots downstream.

Plan: `docs/capstone-plan.md`.

## What exists so far

| Module | Responsibility |
|---|---|
| `bronze.py` | Latest payload per bookmaker for one fixture, keyed by parser name |
| `bookmakers.py` | The one name divergence between bronze and the parsers |

```python
from uuid import UUID
from arbibet_capstone.bronze import connect, latest_payloads

with connect() as conn:
    payloads = latest_payloads(conn, UUID("..."))   # {"bet9ja": b"...", "msports": b"..."}
```

## Two rules this code exists to enforce

**Never filter bronze by `bookmaker` alone.** The indexes are
`(event_id, bookmaker, write_time DESC)`, `(fire_time)` and
`(event_id, bookmaker, payload_sha256)` — none leads with `bookmaker`, so a
book-only filter sequentially scans the BYTEA payload bodies. That query froze
the production host once already.

**`msport` is `msports` to the parsers.** Bronze writes the former, the ported
parser registry keys on the latter, and nothing raises when they disagree —
the book silently yields no markets and EV loses half its probability source.
The rename lives in exactly one place.

## Development

```bash
pip install -e ".[dev]"
pytest          # no database required
ruff check .
mypy
```

Upstream is read-only: `connect()` sets `default_transaction_read_only` on the
session, so a stray write is refused by Postgres rather than by code review.
