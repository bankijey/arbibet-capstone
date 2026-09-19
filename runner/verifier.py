"""The match-verification review queue, shared by the hot loop and the warm loop.

Nothing here excludes a book on its own. The check (arbibet_capstone.verify)
proposes CANDIDATES -- books whose payload names a different match from the
fixture they are filed under -- and records them in `core.fixture_check` and
in a local file for the review dashboard (dashboard/local.py). A person
decides there: `mismatch` (exclude the book from the fixture) or `cleared`
(the names are an alias; leave it). Only a confirmed `mismatch` is applied:
the hot loop stops comparing that book for the fixture, its signals for the
fixture are removed, and every batch job and dbt model skips it.

Why not automatic: the automatic verdicts were wrong often enough -- club
aliases and rebrands the check cannot know -- and a wrong exclusion silently
costs a real signal. A wrong inclusion is what the queue is for, and the
person reviewing sees both books' names side by side.

    check(fixture, payloads)  -> the payloads worth comparing, and the verdicts
    mismatched()              -> event_id -> excluded (confirmed) books
    apply_decisions()         -> read the dashboard's decisions; purge newly confirmed

Decisions arrive as a JSON file the dashboard writes (LOCAL_DIR/decisions.json);
candidates and verdicts go out as LOCAL_DIR/fixture_checks.json.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arbibet_capstone import verify
from arbibet_capstone.crosswalk.parsers import PARSER_REGISTRY
from arbibet_capstone.fixtures import Fixture
from arbibet_capstone.verify import Check, Result
from arbibet_capstone.warehouse import Warehouse, database_path, merge_bulk

log = logging.getLogger("runner.verifier")

# An unverified book is re-checked after this long.
RETRY = timedelta(minutes=10)
# Verdicts a person made; automation never overwrites them.
DECIDED = ("mismatch", "cleared")
# The "book" of a decision about the WHOLE fixture: a subscriber flagged it as a
# wrong match in Telegram (or a reviewer did locally). While it stands, the
# fixture yields no arbitrage and no EV at all.
WHOLE_FIXTURE = "*"


def local_dir() -> Path:
    return Path(os.environ.get("LOCAL_DIR", str(database_path().parent / "local")))


class FixtureVerifier:
    def __init__(self, warehouse: Warehouse) -> None:
        self.warehouse = warehouse
        self.client: Any = None
        self.model = os.environ.get("VERIFY_MODEL", verify.MODEL)
        self.escalate: str | None = (
            os.environ.get("VERIFY_ESCALATE_MODEL", verify.ESCALATE_MODEL) or None
        )
        # Off unless asked for: the names check alone is free and was right
        # where it was sure; the model's guesses about aliases were not.
        if os.environ.get("OPENAI_KEY") and os.environ.get("VERIFY_WITH_MODEL", "0") == "1":
            from openai import OpenAI

            self.client = OpenAI(api_key=os.environ["OPENAI_KEY"], timeout=20.0, max_retries=1)
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Check]] = {}
        self._checked: dict[tuple[str, str], datetime] = {}
        self._fixtures: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._decisions_seen = 0.0
        # Called after a whole-fixture flag changes, so serving is republished at once.
        self.on_change: Any = None
        self.stats = {"checked": 0, "model_calls": 0, "candidates": 0, "decided": 0}

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
            if r.METHOD == "model" and r.MODEL != current and r.VERDICT not in DECIDED:
                continue  # an older prompt's proposal: propose again
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
        """Confirmed exclusions only."""
        with self._lock:
            self._load()
            return {
                e: {b for b, c in checks.items() if c.verdict == "mismatch"}
                for e, checks in self._cache.items()
                if any(c.verdict == "mismatch" for c in checks.values())
            }

    # --- the check -----------------------------------------------------------------

    def check(self, fixture: Fixture, payloads: Mapping[str, Any]) -> tuple[dict[str, Any], Result]:
        """Payloads minus the books a person excluded from `fixture`, and every verdict
        (candidates included, for the queue). A fixture flagged whole yields nothing."""
        event_id = str(fixture.event_id)
        with self._lock:
            self._load()
            whole = self._cache.get(event_id, {}).get(WHOLE_FIXTURE)
            if whole is not None and whole.verdict == "mismatch":
                return {}, Result(checks={WHOLE_FIXTURE: whole})
            known = dict(self._cache.get(event_id, {}))
            now = datetime.now(UTC)
            for book, check in list(known.items()):
                stamp = self._checked.get((event_id, book))
                if check.verdict == "unverified" and not (stamp and now - stamp < RETRY):
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
            # A person's decision stands while the book names the same teams,
            # so it is handed to the check as already known, whatever it found.
            decided = {b: c for b, c in known.items() if c.verdict in DECIDED}
            automatic = {b: c for b, c in known.items() if c.verdict not in DECIDED}
            before = sum(1 for c in automatic.values() if c.method == "model")
            result = verify.verify(info, raw, self.client, automatic, self.model, self.escalate)
            for book, check in result.checks.items():
                # The check's own "mismatch" is only ever a proposal here.
                if check.verdict == "mismatch":
                    result.checks[book] = Check(
                        book,
                        check.home,
                        check.away,
                        "candidate",
                        check.method,
                        check.explanation,
                        check.score,
                    )
                prior = decided.get(book)
                if prior is not None and (prior.home, prior.away) == (check.home, check.away):
                    result.checks[book] = prior
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
            self.stats["candidates"] += sum(1 for c in changed if c.verdict == "candidate")
            if changed:
                merge_bulk(
                    self.warehouse,
                    table="fixture_check",
                    rows=[verify.row(event_id, c, now, self.model) for c in changed],
                    key=["event_id", "bookmaker_name"],
                    keep=["reviewed_at", "note"],
                )
                self._cache.setdefault(event_id, {}).update({c.bookmaker: c for c in changed})
                for c in changed:
                    self._checked[(event_id, c.bookmaker)] = now
            self._fixtures[event_id] = {
                "fixture": f"{fixture.home_team} v {fixture.away_team}",
                "tournament": fixture.tournament,
                "kickoffAt": fixture.kickoff.isoformat(),
            }
            if any(c.verdict == "candidate" for c in changed):
                log.info(
                    "%s v %s: review candidate -- %s",
                    fixture.home_team,
                    fixture.away_team,
                    verify.describe("", (c for c in changed if c.verdict == "candidate")),
                )
                self._write_local()
        excluded = {b for b, c in result.checks.items() if c.verdict == "mismatch"}
        return {b: p for b, p in payloads.items() if b not in excluded}, result

    # --- the local dashboard: out and in ----------------------------------------------------

    def _write_local(self) -> None:
        """Every verdict, for the review dashboard. Never raises."""
        try:
            folder = local_dir()
            folder.mkdir(parents=True, exist_ok=True)
            rows = [
                {
                    "eventId": event_id,
                    **self._fixtures.get(event_id, {}),
                    "book": c.bookmaker,
                    "bookHome": c.home,
                    "bookAway": c.away,
                    "verdict": c.verdict,
                    "method": c.method,
                    "explanation": c.explanation,
                    "checkedAt": self._checked.get(
                        (event_id, c.bookmaker), datetime.now(UTC)
                    ).isoformat(),
                }
                for event_id, checks in self._cache.items()
                for c in checks.values()
                if c.verdict != "ok"
            ]
            fixtures = self.warehouse.query(
                "SELECT event_id, home_team, away_team, tournament, kickoff_at "
                "FROM core.dim_fixture "
                "WHERE event_id IN (SELECT DISTINCT event_id FROM core.fixture_check)"
            )
            names = {
                str(r.EVENT_ID): {
                    "fixture": f"{r.HOME_TEAM} v {r.AWAY_TEAM}",
                    "tournament": r.TOURNAMENT,
                    "kickoffAt": r.KICKOFF_AT.isoformat() if r.KICKOFF_AT is not None else None,
                }
                for r in fixtures.itertuples(index=False)
            }
            for row in rows:
                if "fixture" not in row:
                    row.update(names.get(row["eventId"], {}))
            path = folder / "fixture_checks.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"writtenAt": datetime.now(UTC).isoformat(), "checks": rows}, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception:
            log.warning("could not write the review file", exc_info=True)

    def write_local(self) -> None:
        with self._lock:
            self._load()
            self._write_local()

    def apply_decisions(self) -> int:
        """Read LOCAL_DIR/decisions.json: [{eventId, book, verdict, note, at}]. A newly
        confirmed `mismatch` purges the book's signals for the fixture; `cleared`
        restores the book. Returns the number of decisions applied."""
        path = local_dir() / "decisions.json"
        if not path.exists():
            return 0
        try:
            stamp = path.stat().st_mtime
            if stamp <= self._decisions_seen:
                return 0
            decisions = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("could not read the decisions file", exc_info=True)
            return 0
        applied = 0
        to_purge: list[tuple[str, str]] = []
        with self._lock:
            self._load()
            now = datetime.now(UTC)
            for d in decisions:
                event_id, book, verdict = (
                    str(d.get("eventId")),
                    str(d.get("book")),
                    d.get("verdict"),
                )
                if verdict not in DECIDED:
                    continue
                current = self._cache.get(event_id, {}).get(book)
                if current is not None and current.verdict == verdict:
                    continue
                check = Check(
                    book,
                    current.home if current else d.get("bookHome"),
                    current.away if current else d.get("bookAway"),
                    verdict,
                    "review",
                    str(
                        d.get("note")
                        or ("excluded by review" if verdict == "mismatch" else "cleared by review")
                    ),
                    current.score if current else None,
                )
                row = verify.row(event_id, check, now, self.model)
                row["reviewed_at"] = now
                row["note"] = d.get("note")
                merge_bulk(
                    self.warehouse,
                    table="fixture_check",
                    rows=[row],
                    key=["event_id", "bookmaker_name"],
                )
                self._cache.setdefault(event_id, {})[book] = check
                self._checked[(event_id, book)] = now
                applied += 1
                if verdict == "mismatch":
                    to_purge.append((event_id, book))
            self._decisions_seen = stamp
            self.stats["decided"] += applied
            if applied:
                self._write_local()
        for event_id, book in to_purge:
            self.purge(event_id, book)
        return applied

    # --- whole-fixture flags (Telegram, or the local dashboard) -------------------------------

    def flagged_fixtures(self) -> dict[str, Check]:
        with self._lock:
            self._load()
            return {
                e: checks[WHOLE_FIXTURE]
                for e, checks in self._cache.items()
                if WHOLE_FIXTURE in checks and checks[WHOLE_FIXTURE].verdict == "mismatch"
            }

    def candidates(self, event_id: str) -> dict[str, tuple[str | None, str | None]]:
        """Books awaiting review for the fixture, with what they list: shown on alerts,
        so the reader checks the match before staking. Informational only."""
        with self._lock:
            self._load()
            return {
                b: (c.home, c.away)
                for b, c in self._cache.get(event_id, {}).items()
                if c.verdict == "candidate"
            }

    def book_names(self, event_id: str) -> dict[str, tuple[str | None, str | None]]:
        """What each checked book lists for the fixture, for the person deciding."""
        with self._lock:
            self._load()
            return {
                b: (c.home, c.away)
                for b, c in self._cache.get(event_id, {}).items()
                if b != WHOLE_FIXTURE and (c.home or c.away)
            }

    def flag_fixture(
        self, event_id: str, flagged: bool, by: str, fixture: str | None = None
    ) -> int:
        """Exclude a fixture from arbitrage and EV (or restore it). Returns signals removed."""
        now = datetime.now(UTC)
        verdict = "mismatch" if flagged else "cleared"
        check = Check(
            WHOLE_FIXTURE,
            None,
            None,
            verdict,
            "review",
            f"{'flagged as a wrong match' if flagged else 'restored'} by {by}",
        )
        row = verify.row(event_id, check, now, self.model)
        row["reviewed_at"] = now
        row["note"] = fixture
        with self._lock:
            self._load()
            merge_bulk(
                self.warehouse,
                table="fixture_check",
                rows=[row],
                key=["event_id", "bookmaker_name"],
            )
            self._cache.setdefault(event_id, {})[WHOLE_FIXTURE] = check
            self._checked[(event_id, WHOLE_FIXTURE)] = now
            if fixture:
                self._fixtures.setdefault(event_id, {})["fixture"] = fixture
            self.stats["decided"] += 1
            self._write_local()
        removed = self.purge(event_id, WHOLE_FIXTURE) if flagged else 0
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.warning("change hook failed", exc_info=True)
        return removed

    def purge(self, event_id: str, book: str) -> int:
        """Remove a confirmed wrong book's signals for the fixture: every arbitrage
        signal with a leg from it, and every EV signal at it. `WHOLE_FIXTURE`
        removes every signal the fixture has."""
        removed = 0
        raw = self.warehouse.raw.cursor()
        if book == WHOLE_FIXTURE:
            try:
                for table in ("fact_arbitrage_signal", "fact_ev_signal"):
                    found = raw.execute(
                        f"DELETE FROM core.{table} WHERE event_id = ?", [event_id]
                    ).fetchall()
                    removed += int(found[0][0]) if found and found[0] else 0
            finally:
                raw.close()
            log.info("review: excluded fixture %s; removed %d signals", event_id, removed)
            return removed
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
        log.info("review: excluded %s from %s; removed %d signals", book, event_id, removed)
        return removed


_shared: FixtureVerifier | None = None


def shared(warehouse: Warehouse) -> FixtureVerifier:
    global _shared
    if _shared is None:
        _shared = FixtureVerifier(warehouse)
    return _shared
