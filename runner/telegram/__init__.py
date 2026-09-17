"""The Telegram bot: instant surebet and EV alerts, and the dashboard as commands.

WHERE IT RUNS: two threads inside the runner, not a separate service.

* Alerts must leave the moment the hot loop stores a signal. In-process, the
  hot loop hands its rows straight to the alert thread -- no polling interval,
  no second copy of the signal logic, and nothing to fall out of step.
* A separate process could not read the warehouse at all (DuckDB allows one
  process), so it would have to poll Supabase, where signals arrive up to 30 s
  late, coalesced.
* Commands read only Supabase -- the documents the dashboard reads -- so a
  Telegram user never waits on, or contends with, a warm job holding DuckDB.
  `/health` is the exception: two tiny reads of the runner's own ops tables.

Off unless TELEGRAM_BOT_TOKEN is set (and Supabase is configured). The token
is a secret: it lives in .env and is never logged.

Activity is observable like every other component: a `telegram` heartbeat
every minute, and a `telegram.activity` row in ops.job_run every five minutes
with alerts sent, commands handled, errors and alert lag.
"""

from __future__ import annotations

import logging
import os
import statistics
import threading
from datetime import UTC, datetime
from typing import Any

from arbibet_capstone.warehouse import Warehouse
from runner.observe import Observer

log = logging.getLogger("runner.telegram")

SUMMARY_SECONDS = 300
COUNTERS = (
    "alerts_sent",
    "alerts_surebet",
    "alerts_ev",
    "alerts_dropped",
    "send_errors",
    "alert_errors",
    "commands",
    "callbacks",
    "command_errors",
    "poll_errors",
    "flags_set",
)


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("SUPABASE_DB_URL"))


class TelegramService:
    """Owns the API client, the store, and the alert and command threads."""

    def __init__(self, warehouse: Warehouse, observer: Observer, stop: threading.Event) -> None:
        from runner.telegram.alerts import Alerter
        from runner.telegram.api import TelegramAPI
        from runner.telegram.bot import Bot
        from runner.telegram.store import Store

        self.observer = observer
        self.stop = stop
        self.stats: dict[str, Any] = {}
        self.api = TelegramAPI(os.environ["TELEGRAM_BOT_TOKEN"])
        self.store = Store()
        allowed = {
            int(c)
            for c in os.environ.get("TELEGRAM_ALLOWED_CHATS", "").replace(" ", "").split(",")
            if c
        } or None
        self.alerter = Alerter(self.api, self.store, warehouse, self.stats, stop)
        self.bot = Bot(
            self.api,
            self.store,
            warehouse,
            self.stats,
            stop,
            allowed=allowed,
            on_subscribers_changed=self.alerter.refresh_subscribers,
        )
        self.started_at = datetime.now(UTC)

    # The hot loop's hook.
    def submit(
        self,
        fixture: Any,
        snapshot: Any,
        arb: list[dict],
        ev: list[dict],
        written_at: datetime | None = None,
    ) -> None:
        self.alerter.submit(fixture, snapshot, arb, ev, written_at)

    def start(self) -> None:
        self.store.ensure_schema()
        me = self.api.me()
        log.info("telegram bot @%s started", me.get("username"))
        self.stats["username"] = me.get("username")
        self.alerter.start()
        self.bot.start()
        threading.Thread(target=self._observe, name="telegram-observe", daemon=True).start()

    def _observe(self) -> None:
        # Counters in `stats` only ever grow (they are "since start" for /health);
        # each summary row records the difference since the previous one.
        previous = dict.fromkeys(COUNTERS, 0)
        window_start = datetime.now(UTC)
        while not self.stop.wait(60):
            try:
                subscribers: int | None = len(self.store.subscribers())
                self.stats["subscribers"] = subscribers
            except Exception:
                subscribers = None
            alive = self.alerter.is_alive() and self.bot.is_alive()
            current = {k: self.stats.get(k, 0) for k in COUNTERS}
            self.observer.beat(
                "telegram",
                "running" if alive else "error",
                username=self.stats.get("username"),
                subscribers=subscribers,
                alerts_thread=self.alerter.is_alive(),
                bot_thread=self.bot.is_alive(),
                last_poll=self.stats.get("last_poll"),
                last_error=self.stats.get("last_error"),
                **current,
            )
            now = datetime.now(UTC)
            if (now - window_start).total_seconds() < SUMMARY_SECONDS:
                continue
            window = {k: current[k] - previous[k] for k in COUNTERS}
            previous = current
            lags = self.stats.pop("alert_lag_s", [])
            timings = self.stats.pop("command_ms", [])
            if any(window.values()):
                self.observer.record(
                    "telegram.activity",
                    "telegram",
                    trigger="summary",
                    started_at=window_start,
                    rows_written=window["alerts_sent"],
                    detail={
                        **window,
                        "subscribers": subscribers,
                        "alert_lag_median_s": round(statistics.median(lags), 1) if lags else None,
                        "command_median_ms": round(statistics.median(timings)) if timings else None,
                    },
                )
            window_start = now
