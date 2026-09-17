"""Commands: the dashboard, one message at a time.

    /start /stop      subscribe to alerts, or stop them
    /surebets         upcoming surebet cards: best, now, legs, links, stake split
    /stake            split a stake across the card in view, with your own odds
    /ev [threshold]   upcoming positive EV, with price history
    /flags            legs marked "not on site", and undo
    /slips            popular upcoming and played slips, leg by leg, with the AI verdict
    /dive [search]    fixture deep dive: brief, prices, form, settled markets, punters
    /health           the pipeline's own record
    /settings         alert kinds, EV threshold, default stake, mute

Long lists are pages of one message, turned with buttons that edit it in place.

CALLBACK DATA is limited to 64 bytes, too little for a fixture id plus a market
plus a book. Buttons carry a short token instead, mapped to the action and its
arguments in memory (`Callbacks`). Tokens do not survive a runner restart: a
stale button answers "run the command again" rather than acting on the wrong row.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

from arbibet_capstone.warehouse import Warehouse
from runner.telegram import render
from runner.telegram.api import TelegramAPI, TelegramError
from runner.telegram.model import Subscriber
from runner.telegram.store import Store

log = logging.getLogger("runner.telegram.bot")

EV_PAGE = 5
DIVE_PAGE = 8
DEFAULT_EV = 0.015

COMMANDS = [
    ("surebets", "Upcoming surebets with legs, links and stake split"),
    ("ev", "Upcoming positive EV, e.g. /ev 0.03"),
    ("stake", "Split a stake: /stake 100 [odds ...]"),
    ("slips", "Popular upcoming and played slips"),
    ("dive", "Fixture deep dive, e.g. /dive arsenal"),
    ("flags", "Legs marked not on site"),
    ("health", "Pipeline status"),
    ("settings", "Alerts, EV threshold, stake, mute"),
    ("start", "Subscribe to alerts"),
    ("stop", "Stop alerts"),
    ("help", "What this bot does"),
]

HELP = (
    "🤖 <b>Arbibet signals</b>\n\n"
    "I message you the moment the pipeline finds a <b>surebet</b> (every leg priced "
    "within five minutes, guaranteed return above the stake) or a <b>positive-EV</b> "
    "price at or above your threshold, before kick-off.\n\n"
    "/surebets — upcoming surebets, stake split, arbitrage over time, flag a leg\n"
    "/ev 0.02 — upcoming EV at or above 0.02, with price history\n"
    "/stake 100 2.10 1.95 — split 100 across the card you are viewing\n"
    "/slips — popular slips, upcoming and played, with AI verdicts\n"
    "/dive arsenal — deep dive on a fixture\n"
    "/flags — legs marked not on site\n"
    "/health — pipeline status\n"
    "/settings — alerts, threshold, stake, mute\n"
    "/stop — no more alerts\n\n"
    "<i>Market data, not betting advice. Always check the price on the site.</i>"
)


def button(text: str, token: str) -> dict[str, str]:
    return {"text": text, "callback_data": token}


def url_button(text: str, url: str) -> dict[str, str]:
    return {"text": text, "url": url}


class Callbacks:
    """Short tokens for button payloads, newest 20,000 kept."""

    def __init__(self, size: int = 20_000) -> None:
        self._items: OrderedDict[str, tuple[str, tuple[Any, ...]]] = OrderedDict()
        self._next = 0
        self._size = size
        self._lock = threading.Lock()

    def put(self, action: str, *args: Any) -> str:
        with self._lock:
            self._next += 1
            token = f"t{self._next:x}"
            self._items[token] = (action, args)
            while len(self._items) > self._size:
                self._items.popitem(last=False)
            return token

    def get(self, token: str) -> tuple[str, tuple[Any, ...]] | None:
        with self._lock:
            return self._items.get(token)


def parse_float(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


class Bot(threading.Thread):
    def __init__(
        self,
        api: TelegramAPI,
        store: Store,
        warehouse: Warehouse,
        stats: dict[str, Any],
        stop: threading.Event,
        allowed: set[int] | None = None,
        on_subscribers_changed: Any = None,
    ) -> None:
        super().__init__(name="telegram-bot", daemon=True)
        self.api = api
        self.store = store
        self.warehouse = warehouse
        self.stats = stats
        self.stop = stop
        self.allowed = allowed
        self.on_subscribers_changed = on_subscribers_changed or (lambda: None)
        self.callbacks = Callbacks()
        # The surebet card each chat last looked at, for /stake.
        self.viewing: dict[int, dict[str, Any]] = {}

    # --- plumbing ---------------------------------------------------------------------

    def _count(self, name: str) -> None:
        self.stats[name] = self.stats.get(name, 0) + 1

    def _settings(self, chat_id: int) -> Subscriber:
        return self.store.subscriber(chat_id) or Subscriber(chat_id=chat_id, active=False)

    def reply(
        self, chat_id: int, text: str, keyboard: list | None = None, message_id: int | None = None
    ) -> None:
        if message_id is not None:
            self.api.edit(chat_id, message_id, text, keyboard)
        else:
            self.api.send(chat_id, text, keyboard)

    def _signals(self) -> dict[str, Any]:
        return self.store.document("signals") or {}

    def _slips(self) -> dict[str, Any]:
        return self.store.document("slips") or {}

    # --- dispatch -----------------------------------------------------------------------

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            message = query.get("message") or {}
            chat_id = (message.get("chat") or {}).get("id")
            if chat_id is None or not self._allowed(chat_id):
                self.api.answer(query["id"])
                return
            self._count("callbacks")
            found = self.callbacks.get(query.get("data") or "")
            if found is None:
                self.api.answer(query["id"], "This button has expired. Run the command again.")
                return
            self.api.answer(query["id"])
            action, args = found
            getattr(self, f"on_{action}")(chat_id, message.get("message_id"), *args)
            return

        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None or not text.startswith("/"):
            return
        if not self._allowed(chat_id):
            self.api.send(chat_id, "This bot is private.")
            return
        command, *args = text.split()
        command = command[1:].split("@", 1)[0].lower()
        handler = getattr(self, f"cmd_{command}", None)
        self._count("commands")
        if handler is None:
            self.api.send(chat_id, "Unknown command. /help lists them.")
            return
        user = (message.get("from") or {}).get("username")
        handler(chat_id, args, user)

    def _allowed(self, chat_id: int) -> bool:
        return self.allowed is None or chat_id in self.allowed

    # --- subscription ---------------------------------------------------------------------

    def cmd_start(self, chat_id: int, args: list[str], user: str | None) -> None:
        subscriber = self.store.subscribe(chat_id, user)
        self.on_subscribers_changed()
        self.api.send(
            chat_id,
            HELP
            + f"\n\n✅ Subscribed. Surebet alerts {'on' if subscriber.surebets else 'off'}, EV "
            f"alerts at {subscriber.ev_min:.3f} and above. /settings to change.",
        )

    def cmd_help(self, chat_id: int, args: list[str], user: str | None) -> None:
        self.api.send(chat_id, HELP)

    def cmd_stop(self, chat_id: int, args: list[str], user: str | None) -> None:
        self.store.update(chat_id, active=False)
        self.on_subscribers_changed()
        self.api.send(chat_id, "🔕 Alerts stopped. Commands still work; /start to resume.")

    # --- surebets -------------------------------------------------------------------------

    def cmd_surebets(self, chat_id: int, args: list[str], user: str | None) -> None:
        self.on_surebet(chat_id, None, 0)

    def on_surebet(self, chat_id: int, message_id: int | None, index: int) -> None:
        now = datetime.now(UTC)
        cards = render.surebet_cards(self._signals(), self.store.flags(), now)
        if not cards:
            self.reply(
                chat_id,
                "No upcoming surebets right now. True surebets are rare; you will be alerted "
                "when one appears.",
                message_id=message_id,
            )
            return
        index = max(0, min(index, len(cards) - 1))
        card = cards[index]
        self.viewing[chat_id] = card
        stake = self._settings(chat_id).stake
        ids = (card["eventId"], card["marketId"])
        nav = []
        if index > 0:
            nav.append(button("◀ Prev", self.callbacks.put("surebet", index - 1)))
        nav.append(button(f"{index + 1}/{len(cards)}", self.callbacks.put("surebet", index)))
        if index < len(cards) - 1:
            nav.append(button("Next ▶", self.callbacks.put("surebet", index + 1)))
        keyboard = [
            nav,
            [
                button("💰 Split", self.callbacks.put("split", card)),
                button(
                    "📈 Over time",
                    self.callbacks.put("track", *ids, card["fixture"], card["market"]),
                ),
            ],
            [
                button("🚩 Flag a leg", self.callbacks.put("flag_pick", card)),
                button("🔎 Deep dive", self.callbacks.put("dive", card["eventId"])),
            ],
        ]
        self.reply(
            chat_id, render.surebet_card(card, index, len(cards), stake, now), keyboard, message_id
        )

    def on_split(self, chat_id: int, message_id: int | None, card: dict[str, Any]) -> None:
        self.viewing[chat_id] = card
        stake = self._settings(chat_id).stake
        self.api.send(
            chat_id, render.stake_table(card["legs"], [leg["odds"] for leg in card["legs"]], stake)
        )

    def on_track(
        self,
        chat_id: int,
        message_id: int | None,
        event_id: str,
        market_id: str,
        fixture: str,
        market: str,
    ) -> None:
        self.api.send(
            chat_id,
            render.track_summary(self._signals(), event_id, market_id, f"{fixture} · {market}"),
        )

    def cmd_stake(self, chat_id: int, args: list[str], user: str | None) -> None:
        numbers = [parse_float(a) for a in args]
        if any(n is None for n in numbers):
            self.api.send(
                chat_id,
                "Use numbers: <code>/stake 100</code> or " "<code>/stake 100 2.10 1.95</code>",
            )
            return
        card = self.viewing.get(chat_id)
        stake = numbers[0] if numbers else self._settings(chat_id).stake
        odds = numbers[1:]
        legs = card["legs"] if card else []
        if not odds:
            if not card:
                self.api.send(
                    chat_id,
                    "Open a card with /surebets first, or give the odds: "
                    "<code>/stake 100 2.10 1.95</code>",
                )
                return
            odds = [leg["odds"] for leg in legs]
        elif card and len(odds) != len(legs):
            self.api.send(
                chat_id,
                f"The card in view has {len(legs)} legs; send {len(legs)} "
                "odds in the order shown, or none to use the detected prices.",
            )
            return
        header = (
            f"<b>{render.e(card['fixture'])}</b> · {render.e(card['market'])}\n" if card else ""
        )
        self.api.send(chat_id, header + render.stake_table(legs, odds, stake))

    # --- flags ------------------------------------------------------------------------------

    def on_flag_pick(self, chat_id: int, message_id: int | None, card: dict[str, Any]) -> None:
        rows = [
            [
                button(
                    f"✕ {leg['outcome']} @ {leg['book']}",
                    self.callbacks.put(
                        "flag", card["eventId"], card["marketId"], leg["book"], card["fixture"]
                    ),
                )
            ]
            for leg in card["legs"]
        ]
        self.api.send(
            chat_id,
            f"🚩 Which leg is not on the site?\n<b>{render.e(card['fixture'])}</b> · "
            f"{render.e(card['market'])}\n\nFlagging hides the market at that book here and on "
            "the dashboard, for everyone.",
            rows,
        )

    def on_flag(
        self,
        chat_id: int,
        message_id: int | None,
        event_id: str,
        market_id: str,
        book: str,
        fixture: str,
    ) -> None:
        self.store.set_flag(event_id, market_id, book, True)
        self._count("flags_set")
        self.api.send(
            chat_id,
            f"🚩 Flagged <b>{render.e(book)}</b> on {render.e(fixture)} (market "
            f"<code>{render.e(market_id)}</code>). It is hidden here and on the dashboard. "
            "/flags to undo.",
        )

    def cmd_flags(self, chat_id: int, args: list[str], user: str | None) -> None:
        rows = self.store.flag_rows()[:20]
        if not rows:
            self.api.send(chat_id, "No legs are flagged.")
            return
        names = self._fixture_names()
        lines = ["🚩 <b>Flagged legs</b> (newest 20)", ""]
        keyboard = []
        for n, (event_id, market_id, book, at) in enumerate(rows, start=1):
            fixture = names.get(event_id, event_id[:8])
            lines.append(
                f"{n}. {render.e(fixture)} · market <code>{render.e(market_id)}</code> "
                f"· {render.e(book)} · {render.when(at)}"
            )
            keyboard.append(
                [button(f"↩ Unflag {n}", self.callbacks.put("unflag", event_id, market_id, book))]
            )
        self.api.send(chat_id, "\n".join(lines), keyboard)

    def on_unflag(
        self, chat_id: int, message_id: int | None, event_id: str, market_id: str, book: str
    ) -> None:
        self.store.set_flag(event_id, market_id, book, False)
        self.api.send(
            chat_id, f"↩ Unflagged {render.e(book)} on market <code>{render.e(market_id)}</code>."
        )

    def _fixture_names(self) -> dict[str, str]:
        doc = self._signals()
        names = {
            leg["eventId"]: leg["fixture"] for leg in (doc.get("arbitrage") or {}).get("legs") or []
        }
        names |= {r["eventId"]: r["fixture"] for r in (doc.get("ev") or {}).get("rows") or []}
        return names

    # --- EV ---------------------------------------------------------------------------------

    def cmd_ev(self, chat_id: int, args: list[str], user: str | None) -> None:
        threshold = parse_float(args[0]) if args else None
        if args and (threshold is None or threshold < 0):
            self.api.send(chat_id, "Use a number, e.g. <code>/ev 0.02</code> for 2% and above.")
            return
        if threshold is None:
            subscriber = self.store.subscriber(chat_id)
            threshold = subscriber.ev_min if subscriber else DEFAULT_EV
        self.on_ev(chat_id, None, threshold, 0)

    def on_ev(self, chat_id: int, message_id: int | None, threshold: float, start: int) -> None:
        now = datetime.now(UTC)
        doc = self._signals()
        rows = render.ev_rows(doc, threshold, self.store.flags(), now)
        start = max(0, min(start, max(len(rows) - 1, 0)) // EV_PAGE * EV_PAGE)
        keyboard: list[list[dict[str, str]]] = []
        page = rows[start : start + EV_PAGE]
        if page:
            keyboard.append(
                [
                    button(f"💹 {n}", self.callbacks.put("prices", row))
                    for n, row in enumerate(page, start=1)
                ]
            )
            keyboard.append(
                [
                    button(
                        f"🚩 {n}",
                        self.callbacks.put(
                            "flag", row["eventId"], row["marketId"], row["book"], row["fixture"]
                        ),
                    )
                    for n, row in enumerate(page, start=1)
                ]
            )
            nav = []
            if start > 0:
                nav.append(button("◀ Prev", self.callbacks.put("ev", threshold, start - EV_PAGE)))
            if start + EV_PAGE < len(rows):
                nav.append(button("Next ▶", self.callbacks.put("ev", threshold, start + EV_PAGE)))
            if nav:
                keyboard.append(nav)
        self.reply(
            chat_id, render.ev_page(rows, start, EV_PAGE, threshold, now), keyboard, message_id
        )

    def on_prices(self, chat_id: int, message_id: int | None, row: dict[str, Any]) -> None:
        self.api.send(
            chat_id,
            render.price_summary(self._signals(), row),
            [[button("🔎 Deep dive", self.callbacks.put("dive", row["eventId"]))]],
        )

    # --- slips ------------------------------------------------------------------------------

    def cmd_slips(self, chat_id: int, args: list[str], user: str | None) -> None:
        upcoming, played = render.slip_lists(self._slips(), datetime.now(UTC))
        self.api.send(
            chat_id,
            f"🎟 <b>Popular slips</b> with an AI verdict, most copied first.\n"
            f"Upcoming: {len(upcoming)} · played: {len(played)}",
            [
                [
                    button(
                        f"Upcoming ({len(upcoming)})", self.callbacks.put("slip", "upcoming", 0)
                    ),
                    button(f"Played ({len(played)})", self.callbacks.put("slip", "played", 0)),
                ]
            ],
        )

    def on_slip(self, chat_id: int, message_id: int | None, which: str, index: int) -> None:
        now = datetime.now(UTC)
        doc = self._slips()
        upcoming, played = render.slip_lists(doc, now)
        cards = upcoming if which == "upcoming" else played
        label = "Upcoming" if which == "upcoming" else "Played"
        if not cards:
            self.reply(chat_id, f"No {which} slips with a verdict yet.", message_id=message_id)
            return
        index = max(0, min(index, len(cards) - 1))
        card = cards[index]
        nav = []
        if index > 0:
            nav.append(button("◀ Prev", self.callbacks.put("slip", which, index - 1)))
        nav.append(button(f"{index + 1}/{len(cards)}", self.callbacks.put("slip", which, index)))
        if index < len(cards) - 1:
            nav.append(button("Next ▶", self.callbacks.put("slip", which, index + 1)))
        other = "played" if which == "upcoming" else "upcoming"
        keyboard = [
            nav,
            [
                button("🔎 Dive into a leg", self.callbacks.put("slip_legs", card["shareCode"])),
                button(f"Show {other}", self.callbacks.put("slip", other, 0)),
            ],
        ]
        self.reply(
            chat_id, render.slip(doc, card, index, len(cards), label, now), keyboard, message_id
        )

    def on_slip_legs(self, chat_id: int, message_id: int | None, share_code: str) -> None:
        legs = (self._slips().get("legs") or {}).get(share_code) or []
        seen: dict[str, str] = {}
        for leg in legs:
            seen.setdefault(leg["eventId"], f"{leg.get('home')} v {leg.get('away')}")
        keyboard = [
            [button(name, self.callbacks.put("dive", event_id))] for event_id, name in seen.items()
        ]
        self.api.send(
            chat_id,
            f"🔎 Deep dive into which match of <code>{render.e(share_code)}</code>?",
            keyboard or None,
        )

    # --- deep dives -------------------------------------------------------------------------

    def cmd_dive(self, chat_id: int, args: list[str], user: str | None) -> None:
        self.on_dive_list(chat_id, None, " ".join(args) or None, 0)

    def on_dive_list(
        self, chat_id: int, message_id: int | None, query: str | None, start: int
    ) -> None:
        now = datetime.now(UTC)
        found = render.fixtures(self._slips(), now, query)
        if not found:
            self.reply(
                chat_id,
                f"No slipped fixture matches “{render.e(query)}”."
                if query
                else "No upcoming slipped fixtures right now.",
                message_id=message_id,
            )
            return
        page = found[start : start + DIVE_PAGE]
        keyboard = [
            [
                button(
                    f"{p['fixture']} · {render.when(p['kickoffAt'])}",
                    self.callbacks.put("dive", p["eventId"]),
                )
            ]
            for p in page
        ]
        nav = []
        if start > 0:
            nav.append(button("◀ Prev", self.callbacks.put("dive_list", query, start - DIVE_PAGE)))
        if start + DIVE_PAGE < len(found):
            nav.append(button("Next ▶", self.callbacks.put("dive_list", query, start + DIVE_PAGE)))
        if nav:
            keyboard.append(nav)
        title = f"matching “{render.e(query)}”" if query else "upcoming, most slipped first"
        self.reply(
            chat_id,
            f"🔎 <b>Deep dives</b> · {title} · {start + 1}–" f"{start + len(page)} of {len(found)}",
            keyboard,
            message_id,
        )

    def on_dive(self, chat_id: int, message_id: int | None, event_id: str) -> None:
        dive = self.store.dive(event_id)
        if dive is None:
            self.api.send(
                chat_id,
                "No deep dive published for this fixture. Deep dives exist for "
                "fixtures punters put in popular slips.",
            )
            return
        sections = [
            ("📝 Brief", "brief"),
            ("🏁 Post-match", "result"),
            ("💹 Prices", "prices"),
            ("📊 Form", "form"),
            ("✅ Settled", "settled"),
            ("👥 Punters", "punters"),
        ]
        keyboard = [
            [
                button(label, self.callbacks.put("dive_part", event_id, part))
                for label, part in sections[:3]
            ],
            [
                button(label, self.callbacks.put("dive_part", event_id, part))
                for label, part in sections[3:]
            ],
        ]
        self.api.send(chat_id, render.dive_overview(dive, datetime.now(UTC)), keyboard)

    def on_dive_part(self, chat_id: int, message_id: int | None, event_id: str, part: str) -> None:
        dive = self.store.dive(event_id)
        if dive is None:
            self.api.send(chat_id, "This deep dive is no longer published.")
            return
        text = {
            "brief": lambda: render.dive_note(dive, "brief"),
            "result": lambda: render.dive_note(dive, "result"),
            "prices": lambda: render.dive_prices(dive),
            "form": lambda: render.dive_form(dive),
            "settled": lambda: render.dive_settled(dive),
            "punters": lambda: render.dive_punters(dive),
        }[part]()
        self.api.send(chat_id, text)

    # --- health -----------------------------------------------------------------------------

    def cmd_health(self, chat_id: int, args: list[str], user: str | None) -> None:
        now = datetime.now(UTC)
        beats = self.warehouse.query(
            "SELECT component, beat_at, state FROM ops.heartbeat ORDER BY component"
        )
        hot = self.warehouse.query(
            "SELECT detail FROM ops.job_run WHERE job = 'hot.signals' AND started_at > %s",
            (now - timedelta(hours=1),),
        )
        medians, maxima = [], []
        for detail in hot.DETAIL:
            d = json.loads(detail) if isinstance(detail, str) else (detail or {})
            if d.get("latency_median_s") is not None:
                medians.append(d["latency_median_s"])
            if d.get("latency_max_s") is not None:
                maxima.append(d["latency_max_s"])
        failed = int(
            self.warehouse.query(
                "SELECT count(*) AS n FROM ops.job_run WHERE status = 'failed' AND started_at > %s",
                (now - timedelta(hours=24),),
            ).N.iloc[0]
        )
        warm = self.warehouse.query("SELECT detail FROM ops.heartbeat WHERE component = 'warm'")
        warm_detail = None
        if not warm.empty and isinstance(warm.DETAIL.iloc[0], str | dict):
            d = warm.DETAIL.iloc[0]
            d = json.loads(d) if isinstance(d, str) else d
            warm_detail = {
                "seconds": d.get("last_cycle_seconds"),
                "finished_at": d.get("finished_at"),
            }
        medians.sort()
        latency = {
            "median": medians[len(medians) // 2] if medians else None,
            "max": max(maxima) if maxima else None,
        }
        records = [
            {
                "component": c,
                "beat_at": b.to_pydatetime() if hasattr(b, "to_pydatetime") else b,
                "state": s,
            }
            for c, b, s in zip(beats.COMPONENT, beats.BEAT_AT, beats.STATE, strict=True)
        ]
        self.api.send(
            chat_id, render.health(records, latency, failed, warm_detail, self.stats, now)
        )

    # --- settings ---------------------------------------------------------------------------

    def cmd_settings(self, chat_id: int, args: list[str], user: str | None) -> None:
        subscriber = self.store.subscriber(chat_id)
        if subscriber is None:
            subscriber = self.store.subscribe(chat_id, user)
            self.on_subscribers_changed()
        if len(args) == 2 and args[0].lower() in ("ev", "stake"):
            value = parse_float(args[1])
            if value is None or value <= 0 or (args[0].lower() == "ev" and value > 1):
                self.api.send(
                    chat_id,
                    "Use e.g. <code>/settings ev 0.02</code> or "
                    "<code>/settings stake 50</code>.",
                )
                return
            column = "ev_min" if args[0].lower() == "ev" else "stake"
            subscriber = self.store.update(chat_id, **{column: value}) or subscriber
            self.on_subscribers_changed()
        self.on_settings(chat_id, None, None)

    def on_settings(self, chat_id: int, message_id: int | None, change: str | None) -> None:
        subscriber = self.store.subscriber(chat_id)
        if subscriber is None:
            self.reply(chat_id, "Not subscribed. /start first.", message_id=message_id)
            return
        now = datetime.now(UTC)
        updates: dict[str, Any] = {}
        if change == "surebets":
            updates["surebets"] = not subscriber.surebets
        elif change == "ev":
            updates["ev"] = not subscriber.ev
        elif change == "ev_down":
            updates["ev_min"] = max(0.01, round(subscriber.ev_min - 0.005, 3))
        elif change == "ev_up":
            updates["ev_min"] = min(0.5, round(subscriber.ev_min + 0.005, 3))
        elif change in ("mute1", "mute8"):
            updates["muted_until"] = now + timedelta(hours=1 if change == "mute1" else 8)
        elif change == "unmute":
            updates["muted_until"] = None
        elif change == "active":
            updates["active"] = not subscriber.active
        if updates:
            subscriber = self.store.update(chat_id, **updates) or subscriber
            self.on_subscribers_changed()
        muted = subscriber.muted(now)
        text = (
            "⚙️ <b>Settings</b>\n\n"
            f"Alerts: <b>{'on' if subscriber.active else 'off'}</b>"
            + (f" · muted until {render.when(subscriber.muted_until)}" if muted else "")
            + f"\nSurebet alerts: <b>{'on' if subscriber.surebets else 'off'}</b>"
            f"\nEV alerts: <b>{'on' if subscriber.ev else 'off'}</b> at "
            f"<b>{subscriber.ev_min:.3f}</b> and above"
            f"\nDefault stake: <b>{render.money(subscriber.stake)}</b>\n\n"
            "Set exactly: <code>/settings ev 0.02</code> · <code>/settings stake 50</code>"
        )
        put = self.callbacks.put
        keyboard = [
            [
                button(
                    f"Alerts {'on ✅' if subscriber.active else 'off ❌'}",
                    put("settings", "active"),
                )
            ],
            [
                button(
                    f"Surebets {'✅' if subscriber.surebets else '❌'}", put("settings", "surebets")
                ),
                button(f"EV {'✅' if subscriber.ev else '❌'}", put("settings", "ev")),
            ],
            [
                button("EV −0.005", put("settings", "ev_down")),
                button("EV +0.005", put("settings", "ev_up")),
            ],
            [
                button("Unmute", put("settings", "unmute"))
                if muted
                else button("Mute 1 h", put("settings", "mute1")),
                button("Mute 8 h", put("settings", "mute8")),
            ],
        ]
        self.reply(chat_id, text, keyboard, message_id)

    # --- loop -------------------------------------------------------------------------------

    def run(self) -> None:
        offset: int | None = None
        while not self.stop.is_set():
            try:
                self.api.set_commands(COMMANDS)
                break
            except Exception:
                log.warning("could not reach Telegram; retrying", exc_info=True)
                self.stop.wait(30)
        while not self.stop.is_set():
            try:
                updates = self.api.get_updates(offset)
            except TelegramError as err:
                self.stats["poll_errors"] = self.stats.get("poll_errors", 0) + 1
                self.stats["last_error"] = str(err)[:300]
                log.warning("telegram poll failed: %s", err)
                self.stop.wait(10 if err.code != 409 else 60)
                continue
            self.stats["last_poll"] = datetime.now(UTC).isoformat()
            for update in updates:
                offset = update["update_id"] + 1
                started = time.monotonic()
                try:
                    self.handle_update(update)
                except Exception as err:
                    self._count("command_errors")
                    self.stats["last_error"] = str(err)[:300]
                    log.error("telegram update failed", exc_info=True)
                    chat = (
                        (
                            update.get("message")
                            or (update.get("callback_query") or {}).get("message")
                            or {}
                        ).get("chat")
                        or {}
                    ).get("id")
                    if chat is not None:
                        try:
                            self.api.send(
                                chat, "Something went wrong with that. It has been logged."
                            )
                        except Exception:
                            pass
                self.stats.setdefault("command_ms", []).append(
                    round((time.monotonic() - started) * 1000)
                )
