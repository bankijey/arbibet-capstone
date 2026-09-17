"""The match-verification gate, shared by the hot loop and the warm loop.

One instance per runner (`shared`), because both loops must agree on which
books are excluded from which fixtures, and the model should be asked once.

    check(fixture, payloads)  -> the payloads worth comparing, and the verdicts
    mismatched()              -> event_id -> excluded books, for batch jobs
    purge(event_id, book)     -> remove a wrong book's signals for a fixture

Verdicts persist in `core.fixture_check`, so a restart does not re-ask the
model and the dashboard can show why a signal is gone.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from arbibet_capstone import verify
from arbibet_capstone.crosswalk.parsers import PARSER_REGISTRY
from arbibet_capstone.fixtures import Fixture
from arbibet_capstone.verify import Check, Result
from arbibet_capstone.warehouse import Warehouse, merge_bulk

log = logging.getLogger("runner.verifier")

# An unverified book is re-checked after this long.
RETRY = timedelta(minutes=10)


class FixtureVerifier:
    def __init__(
        self, warehouse: Warehouse, on_mismatch: Callable[[Fixture, Check], None] | None = None
    ) -> None:
        self.warehouse = warehouse
        self.on_mismatch = on_mismatch
        self.client: Any = None
        self.model = os.environ.get("VERIFY_MODEL", verify.MODEL)
        if os.environ.get("OPENAI_KEY") and os.environ.get("VERIFY_WITH_MODEL", "1") == "1":
            from openai import OpenAI

            self.client = OpenAI(api_key=os.environ["OPENAI_KEY"], timeout=20.0, max_retries=1)
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Check]] = {}
        self._checked: dict[tuple[str, str], datetime] = {}
        self._loaded = False
        self.stats = {"checked": 0, "model_calls": 0, "mismatches": 0}

    # --- state -------------------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        frame = self.warehouse.query(
            "SELECT event_id, bookmaker_name, checked_at, verdict, book_home, book_away, method, "
            "explanation, model FROM core.fixture_check"
        )
        current = f"{self.model}#{verify.PROMPT_VERSION}"
        for r in frame.itertuples(index=False):
            if r.METHOD == "model" and r.MODEL != current:
                continue  # an older prompt's verdict: ask again
            self._cache.setdefault(str(r.EVENT_ID), {})[str(r.BOOKMAKER_NAME)] = Check(
                str(r.BOOKMAKER_NAME),
                None if r.BOOK_HOME is None else str(r.BOOK_HOME),
                None if r.BOOK_AWAY is None else str(r.BOOK_AWAY),
                str(r.VERDICT),
                str(r.METHOD),
                str(r.EXPLANATION or ""),
            )
            self._checked[(str(r.EVENT_ID), str(r.BOOKMAKER_NAME))] = (
                r.CHECKED_AT.to_pydatetime()
                if hasattr(r.CHECKED_AT, "to_pydatetime")
                else r.CHECKED_AT
            )
        self._loaded = True
        log.info("fixture checks loaded: %d fixtures", len(self._cache))

    def mismatched(self) -> dict[str, set[str]]:
        with self._lock:
            self._load()
            return {
                e: {b for b, c in checks.items() if c.verdict == "mismatch"}
                for e, checks in self._cache.items()
                if any(c.verdict == "mismatch" for c in checks.values())
            }

    # --- the gate ------------------------------------------------------------------

    def check(self, fixture: Fixture, payloads: Mapping[str, Any]) -> tuple[dict[str, Any], Result]:
        """Payloads from books verified to be pricing `fixture`, and every verdict."""
        event_id = str(fixture.event_id)
        with self._lock:
            self._load()
            known = dict(self._cache.get(event_id, {}))
            now = datetime.now(UTC)
            # An unverified verdict is reused only briefly, so the model is retried.
            for book, check in list(known.items()):
                stamp = self._checked.get((event_id, book))
                if check.verdict == "unverified" and stamp and now - stamp < RETRY:
                    continue
                if check.verdict == "unverified":
                    known.pop(book)
            info = {
                "home": fixture.home_team,
                "away": fixture.away_team,
                "tournament": fixture.tournament,
                "kickoff": fixture.kickoff.isoformat(),
                "book_names": fixture.book_names or {},
            }
            # Only the books whose prices are compared; bronze holds others
            # (betking, ...) that no parser reads and no signal uses.
            raw = {b: p.payload for b, p in payloads.items() if b in PARSER_REGISTRY}
            before = sum(1 for c in known.values() if c.method == "model")
            result = verify.verify(info, raw, self.client, known, self.model)
            self.stats["checked"] += 1
            self.stats["model_calls"] += max(
                0, sum(1 for c in result.checks.values() if c.method == "model") - before
            )
            changed = [
                c
                for b, c in result.checks.items()
                if known.get(b) is None
                or (known[b].verdict, known[b].explanation) != (c.verdict, c.explanation)
            ]
            if changed:
                merge_bulk(
                    self.warehouse,
                    table="fixture_check",
                    rows=[verify.row(event_id, c, now, self.model) for c in changed],
                    key=["event_id", "bookmaker_name"],
                )
                self._cache.setdefault(event_id, {}).update({c.bookmaker: c for c in changed})
                for c in changed:
                    self._checked[(event_id, c.bookmaker)] = now
            newly_wrong = [
                c
                for c in changed
                if c.verdict == "mismatch"
                and (known.get(c.bookmaker) is None or known[c.bookmaker].verdict != "mismatch")
            ]
        for c in newly_wrong:
            self.stats["mismatches"] += 1
            log.warning(
                "%s v %s: %s excluded -- %s",
                fixture.home_team,
                fixture.away_team,
                c.bookmaker,
                c.explanation,
            )
            self.purge(event_id, c.bookmaker)
            if self.on_mismatch:
                try:
                    self.on_mismatch(fixture, c)
                except Exception:
                    log.warning("mismatch hook failed", exc_info=True)
        accepted = {b: p for b, p in payloads.items() if b not in result.checks or b in result.ok}
        return accepted, result

    def purge(self, event_id: str, book: str) -> int:
        """Remove the wrong book's signals for the fixture: every arbitrage signal
        with a leg from it, and every EV signal at it."""
        removed = 0
        raw = self.warehouse.raw.cursor()
        try:
            rows = raw.execute(
                "SELECT signal_key, legs::VARCHAR FROM core.fact_arbitrage_signal "
                "WHERE event_id = ?",
                [event_id],
            ).fetchall()
            # DuckDB may re-serialise the JSON without spaces; compare without them.
            needle = f'"bookmaker":"{book}"'
            keys = [k for k, legs in rows if needle in (legs or "").replace(" ", "")]
            for start in range(0, len(keys), 500):
                batch = keys[start : start + 500]
                raw.execute(
                    "DELETE FROM core.fact_arbitrage_signal WHERE signal_key IN "
                    f"({', '.join('?' for _ in batch)})",
                    batch,
                )
                removed += len(batch)
            ev = raw.execute(
                "DELETE FROM core.fact_ev_signal WHERE event_id = ? AND bookmaker_id = "
                "(SELECT bookmaker_id FROM core.dim_bookmaker WHERE bookmaker_name = ?)",
                [event_id, book],
            ).fetchall()
            removed += int(ev[0][0]) if ev and ev[0] else 0
        finally:
            raw.close()
        if removed:
            log.info("removed %d signals of %s for %s", removed, book, event_id)
        return removed


_shared: FixtureVerifier | None = None


def shared(
    warehouse: Warehouse, on_mismatch: Callable[[Fixture, Check], None] | None = None
) -> FixtureVerifier:
    global _shared
    if _shared is None:
        _shared = FixtureVerifier(warehouse, on_mismatch)
    elif on_mismatch is not None:
        _shared.on_mismatch = on_mismatch
    return _shared
