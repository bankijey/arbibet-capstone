"""Push alerts: from the hot loop's recompute to subscribers' phones, in seconds.

The hot loop calls `Alerter.submit` with what it just computed for a fixture
-- the same rows it stored -- and returns at once: the work here (names, links,
the ledger, sending) happens on this thread, so a slow Telegram never slows a
signal. Nothing is polled: an alert leaves as soon as the signal exists.

Names come from where they are cheapest. Outcome names from the parsed
snapshot the hot loop already holds; market names from `dim_market`, cached;
bet links from `dim_event_link`, read only when someone is actually due an
alert. Those two small reads are the only warehouse access, in-process.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from arbibet_capstone.fixtures import Fixture
from arbibet_capstone.warehouse import Warehouse
from runner.telegram import render
from runner.telegram.api import TelegramAPI, TelegramError
from runner.telegram.model import (
    Ledger,
    Leg,
    Opportunity,
    best_per_key,
    eligible,
    outcome_names,
    recipients,
)
from runner.telegram.store import Store
from runner.telegram.wallet import size_ev, size_surebet

log = logging.getLogger("runner.telegram.alerts")

# An EV this large is a mis-mapped or mispriced market, not an edge: the
# published EV list's top rows (+180% on odd/even) are exactly that.
EV_ALERT_MAX = float(os.environ.get("TELEGRAM_EV_MAX", "0.5"))
NAMES_TTL = 600.0
SUBSCRIBERS_TTL = 30.0


class Alerter(threading.Thread):
    def __init__(
        self,
        api: TelegramAPI,
        store: Store,
        warehouse: Warehouse,
        stats: dict[str, Any],
        stop: threading.Event,
    ) -> None:
        super().__init__(name="telegram-alerts", daemon=True)
        self.api = api
        self.store = store
        self.warehouse = warehouse
        self.stats = stats
        self.stop = stop
        self.queue: queue.Queue[tuple[Any, ...]] = queue.Queue(1000)
        self.ledger = Ledger()
        # The bot's button-token table, shared so alert buttons reach its handlers.
        self.callbacks: Any = None
        self._markets: tuple[float, dict[str, str]] = (0.0, {})
        self._subscribers: tuple[float, list[Any]] = (0.0, [])

    # --- called from the hot loop -------------------------------------------------------

    def submit(
        self,
        fixture: Fixture,
        snapshot: Any,
        arb: list[dict],
        ev: list[dict],
        written_at: datetime | None = None,
    ) -> None:
        """Hand over one recompute. Never blocks and never raises.

        `written_at` is when bronze stored the payload that triggered it: alert
        lag is measured from there, like the hot loop's own latency. (From the
        book's fire_time it would include the collectors' delay in writing.)
        """
        if not any(r.get("arbitrage", 0) > 1 for r in arb) and not ev:
            return
        try:
            self.queue.put_nowait((fixture, snapshot, arb, ev, written_at))
        except queue.Full:
            self.stats["alerts_dropped"] = self.stats.get("alerts_dropped", 0) + 1

    # --- names ---------------------------------------------------------------------------

    def _market_names(self) -> dict[str, str]:
        checked, names = self._markets
        if time.monotonic() - checked > NAMES_TTL:
            frame = self.warehouse.query("SELECT market_base_id, market_name FROM core.dim_market")
            names = {
                str(k): str(v) for k, v in zip(frame.MARKET_BASE_ID, frame.MARKET_NAME, strict=True)
            }
            self._markets = (time.monotonic(), names)
        return names

    def _links(self, event_id: str) -> dict[str, str]:
        frame = self.warehouse.query(
            "SELECT b.bookmaker_name, l.url FROM core.dim_event_link l "
            "JOIN core.dim_bookmaker b USING (bookmaker_id) WHERE l.event_id = %s",
            (event_id,),
        )
        return {str(b): str(u) for b, u in zip(frame.BOOKMAKER_NAME, frame.URL, strict=True)}

    def _subscribers_now(self) -> list[Any]:
        checked, subscribers = self._subscribers
        if time.monotonic() - checked > SUBSCRIBERS_TTL:
            subscribers = self.store.subscribers()
            self._subscribers = (time.monotonic(), subscribers)
        return subscribers

    def refresh_subscribers(self) -> None:
        self._subscribers = (0.0, [])

    # --- building --------------------------------------------------------------------------

    def opportunities(
        self, fixture: Fixture, snapshot: Any, arb: list[dict], ev: list[dict]
    ) -> list[Opportunity]:
        markets = self._market_names()
        names = outcome_names({b: q.markets for b, q in snapshot.books.items()})
        title = (
            f"{fixture.home_team} v {fixture.away_team}"
            if fixture.home_team and fixture.away_team
            else str(fixture.event_id)
        )

        def market(row: dict[str, Any]) -> str:
            name = markets.get(str(row["market_base_id"]), f"market {row['market_base_id']}")
            return render.market_label(name, row.get("specifier"))

        found: list[Opportunity] = []
        for row in arb:
            if row["arbitrage"] <= 1:
                continue
            found.append(
                Opportunity(
                    kind="surebet",
                    event_id=row["event_id"],
                    market_id=row["market_id"],
                    fixture=title,
                    tournament=fixture.tournament,
                    kickoff=fixture.kickoff,
                    market=market(row),
                    value=float(row["arbitrage"]),
                    legs=tuple(
                        Leg(
                            outcome=names.get(
                                (row["market_id"], leg["outcome_id"]),
                                f"outcome {leg['outcome_id']}",
                            ),
                            book=leg["bookmaker"],
                            odds=float(leg["odds"]),
                            outcome_id=str(leg["outcome_id"]),
                        )
                        for leg in row["legs"]
                    ),
                    detected_at=row["detected_at"],
                    spread_seconds=row.get("leg_spread_seconds"),
                )
            )
        for row in ev:
            found.append(
                Opportunity(
                    kind="ev",
                    event_id=row["event_id"],
                    market_id=row["market_id"],
                    fixture=title,
                    tournament=fixture.tournament,
                    kickoff=fixture.kickoff,
                    market=market(row),
                    value=float(row["ev"]),
                    legs=(
                        Leg(
                            outcome=row.get("outcome_name") or f"outcome {row['outcome_id']}",
                            book=row["bookmaker"],
                            odds=float(row["odds"]),
                            outcome_id=str(row["outcome_id"]),
                        ),
                    ),
                    detected_at=row["detected_at"],
                    probability=row.get("implied_p"),
                    p_source=row.get("p_source"),
                    outcome_id=str(row["outcome_id"]),
                )
            )
        return best_per_key(found)

    # --- sizing and buttons ------------------------------------------------------------------

    def sizing(self, subscriber: Any, opp: Opportunity) -> Any:
        """Stakes from the subscriber's real balances; None for one who has set none."""
        balances = self.store.balances(subscriber.chat_id, "real")
        if not balances:
            return None
        if opp.kind == "surebet":
            return size_surebet(
                [leg.odds for leg in opp.legs],
                [leg.book for leg in opp.legs],
                balances,
                subscriber.stake,
            )
        leg = opp.legs[0]
        return size_ev(
            opp.value, leg.odds, leg.book, balances, sum(balances.values()), subscriber.stake
        )

    def keyboard(self, opp: Opportunity, signed: bool) -> list[list[dict[str, str]]]:
        if self.callbacks is None:
            return []
        put = self.callbacks.put
        if signed:
            return [
                [
                    {"text": "✅ Placed", "callback_data": put("placed", opp, "real")},
                    {"text": "📝 Paper", "callback_data": put("placed", opp, "paper")},
                ],
                [
                    {"text": "✏️ Odds changed", "callback_data": put("odds", opp)},
                    {"text": "🚩 Not on site", "callback_data": put("missing", opp)},
                ],
            ]
        return [
            [
                {"text": "📝 Paper bet", "callback_data": put("placed", opp, "paper")},
                {"text": "🚩 Not on site", "callback_data": put("missing", opp)},
            ]
        ]

    # --- sending ---------------------------------------------------------------------------

    def handle(
        self,
        fixture: Fixture,
        snapshot: Any,
        arb: list[dict],
        ev: list[dict],
        written_at: datetime | None = None,
    ) -> int:
        now = datetime.now(UTC)
        subscribers = self._subscribers_now()
        if not subscribers:
            return 0
        flagged = self.store.flags()
        sent = 0
        links: dict[str, str] | None = None
        for opp in self.opportunities(fixture, snapshot, arb, ev):
            if not eligible(opp, now, flagged, EV_ALERT_MAX):
                continue
            due = recipients(opp, subscribers, self.ledger, now)
            if not due:
                continue
            if links is None:
                links = self._links(opp.event_id)
            opp = replace(
                opp,
                legs=tuple(replace(leg, url=links.get(leg.book)) for leg in opp.legs),
                p_source_url=links.get(opp.p_source) if opp.p_source else None,
            )
            for subscriber in due:
                try:
                    sizing = self.sizing(subscriber, opp)
                    self.api.send(
                        subscriber.chat_id,
                        render.alert(opp, now, sizing),
                        self.keyboard(opp, signed=sizing is not None),
                    )
                except TelegramError as err:
                    self.stats["send_errors"] = self.stats.get("send_errors", 0) + 1
                    self.stats["last_error"] = str(err)[:300]
                    log.warning("alert to %s failed: %s", subscriber.chat_id, err)
                    if err.code == 403:
                        # The user blocked the bot or deleted the chat.
                        self.store.update(subscriber.chat_id, active=False)
                        self.refresh_subscribers()
                    continue
                self.ledger.mark(subscriber.chat_id, opp)
                self.store.record_alert(subscriber.chat_id, opp.key, opp.value)
                sent += 1
                kind = "alerts_surebet" if opp.kind == "surebet" else "alerts_ev"
                self.stats[kind] = self.stats.get(kind, 0) + 1
                self.stats["alerts_sent"] = self.stats.get("alerts_sent", 0) + 1
                if written_at is not None:
                    lag = (datetime.now(UTC) - written_at).total_seconds()
                    self.stats.setdefault("alert_lag_s", []).append(round(lag, 1))
        return sent

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.ledger = self.store.ledger()
                break
            except Exception:
                log.warning("could not load the alert ledger; retrying", exc_info=True)
                self.stop.wait(30)
        last_prune = time.monotonic()
        while not self.stop.is_set():
            try:
                item = self.queue.get(timeout=5)
            except queue.Empty:
                item = None
            try:
                if item is not None:
                    self.handle(*item)
                if time.monotonic() - last_prune > 3600:
                    self.store.prune_alerts()
                    last_prune = time.monotonic()
            except Exception as err:
                self.stats["alert_errors"] = self.stats.get("alert_errors", 0) + 1
                self.stats["last_error"] = str(err)[:300]
                log.error("alert handling failed", exc_info=True)
                self.stop.wait(2)
