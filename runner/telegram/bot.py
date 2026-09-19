"""Commands: the dashboard, one message at a time.

    /start /stop      subscribe to alerts, or stop them
    /surebets         upcoming surebet cards: best, now, legs, links, stake split
    /stake            split a stake across the card in view, with your own odds
    /ev [threshold]   upcoming positive EV, with price history
    /flags            legs marked "not on site" and fixtures flagged as wrong matches; undo
    /slips            popular upcoming and played slips, leg by leg, with the AI verdict
    /dive [search]    fixture deep dive: brief, prices, form, settled markets, punters
    /health           the pipeline's own record
    /settings         alert kinds, EV threshold, default stake, mute
    /balance          cash at each bookmaker; what stakes are sized from
    /wallet           balances, open bets, locked-in and settled profit; /wallet paper
    /placed /odds     record a bet from an alert, correct its stakes, re-split on new odds

ACCESS. Anyone may read. Sizing, the wallet and "Placed" are for SIGNED users:
a subscriber who has set at least one real balance. The owner (TELEGRAM_OWNER_CHAT)
also has /admin. Anything heavier than a screen -- price charts, form tables,
settled markets -- is a link to the dashboard rather than a wall of text here.

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
from runner.telegram.model import Leg as OppLeg
from runner.telegram.model import Opportunity, Subscriber
from runner.telegram.store import Store
from runner.telegram.wallet import (
    PAPER_START,
    Leg,
    bet_row,
    parse_stakes,
    size_ev,
    size_surebet,
    summary,
)

log = logging.getLogger("runner.telegram.bot")

EV_PAGE = 5
DIVE_PAGE = 8
DEFAULT_EV = 0.015
DASHBOARD = "https://arbibet.streamlit.app"
BOOKS = ("bet9ja", "sportybet", "livescorebet", "msport", "ilotbet")

COMMANDS = [
    ("surebets", "Upcoming surebets with legs, links and stake split"),
    ("ev", "Upcoming positive EV, e.g. /ev 0.03"),
    ("wallet", "Balances, open bets, profit; /wallet paper"),
    ("balance", "Set cash at a book: /balance msport 50000"),
    ("placed", "Correct the stakes of your last recorded bet"),
    ("odds", "Re-split on the site's odds: /odds 2.05 1.98"),
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
    "/balance msport 50000 — your cash at a book; alerts are then sized to it\n"
    "/wallet — balances, open bets, locked-in and settled profit (/wallet paper)\n"
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


def event_url(dashboard: str, event_id: str) -> str:
    return f"{dashboard.rstrip('/')}/fixture?event_id={event_id}"


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
        owner: int | None = None,
        dashboard: str = DASHBOARD,
    ) -> None:
        super().__init__(name="telegram-bot", daemon=True)
        self.api = api
        self.store = store
        self.warehouse = warehouse
        self.stats = stats
        self.stop = stop
        self.allowed = allowed
        self.on_subscribers_changed = on_subscribers_changed or (lambda: None)
        self.owner = owner
        self.dashboard = dashboard.rstrip("/")
        self.callbacks = Callbacks()
        # The surebet card each chat last looked at, for /stake.
        self.viewing: dict[int, dict[str, Any]] = {}
        # The opportunity a chat was asked to send new odds for, and its last recorded bet.
        self.pending_odds: dict[int, Opportunity] = {}
        self.last_bet: dict[int, int] = {}

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
            f"alerts at {subscriber.ev_min:.3f} and above. /settings to change.\n"
            "Tell me your cash at each book (<code>/balance msport 50000</code>) and every "
            "alert arrives with stakes sized to it.",
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
                url_button("📈 History ↗", self.event_url(card["eventId"])),
            ],
            [
                button(
                    "✅ Placed", self.callbacks.put("placed", self._card_opportunity(card), "real")
                ),
                button(
                    "📝 Paper", self.callbacks.put("placed", self._card_opportunity(card), "paper")
                ),
            ],
            [
                button("🚩 Flag a leg", self.callbacks.put("flag_pick", card)),
                button("🔎 Deep dive", self.callbacks.put("dive", card["eventId"])),
            ],
            [
                button(
                    "⚠️ Wrong match",
                    self.callbacks.put("wrong_match", card["eventId"], card["fixture"]),
                )
            ],
        ]
        self.reply(
            chat_id, render.surebet_card(card, index, len(cards), stake, now), keyboard, message_id
        )

    @staticmethod
    def _card_opportunity(card: dict[str, Any]) -> Opportunity:
        """A card's newest detection as an Opportunity, so it can be placed like an alert."""
        odds = [leg["odds"] for leg in card["legs"]]
        return Opportunity(
            kind="surebet",
            event_id=card["eventId"],
            market_id=card["marketId"],
            fixture=card["fixture"],
            tournament=None,
            kickoff=render._dt(card["kickoffAt"]) or datetime.now(UTC),
            market=card["market"],
            value=1.0 / sum(1.0 / o for o in odds) if all(o > 0 for o in odds) else 0.0,
            legs=tuple(
                OppLeg(
                    leg["outcome"], leg["book"], leg["odds"], leg.get("url"), leg.get("outcomeId")
                )
                for leg in card["legs"]
            ),
            detected_at=render._dt(card["newestAt"]) or datetime.now(UTC),
        )

    def on_split(self, chat_id: int, message_id: int | None, card: dict[str, Any]) -> None:
        self.viewing[chat_id] = card
        stake = self._settings(chat_id).stake
        self.api.send(
            chat_id, render.stake_table(card["legs"], [leg["odds"] for leg in card["legs"]], stake)
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

    # --- wrong matches ------------------------------------------------------------------------
    #
    # The matcher upstream sometimes files two matches under one fixture, and
    # then two unrelated prices read as a surebet. A signed user who opens the
    # links and sees different matches can say so: the fixture then yields no
    # arbitrage and no EV for anyone, its stored signals are removed, and the
    # decision shows on the local review dashboard, where it can be reversed.

    def _verifier(self) -> Any:
        from runner.verifier import shared

        return shared(self.warehouse)

    def on_wrong_match(
        self, chat_id: int, message_id: int | None, event_id: str, fixture: str
    ) -> None:
        if not self._signed(chat_id):
            self.api.send(
                chat_id,
                "Flagging a wrong match removes the fixture for everyone, so it needs a signed "
                "user: set a balance first (<code>/balance msport 50000</code>).",
            )
            return
        lines = [
            f"⚠️ <b>Wrong match?</b>\n<b>{render.e(fixture)}</b>",
            "",
            "Flag it if the bookmakers' pages are DIFFERENT matches. The fixture then stops "
            "appearing as arbitrage or EV for everyone and its signals are removed. "
            "/flags undoes it.",
        ]
        names = self._verifier().book_names(event_id)
        if names:
            lines += ["", "What each book lists under this fixture:"]
            lines += [
                f"• {render.e(book)}: {render.e(home)} v {render.e(away)}"
                for book, (home, away) in sorted(names.items())
            ]
        self.api.send(
            chat_id,
            "\n".join(lines),
            [
                [
                    button(
                        "⚠️ Yes, wrong match",
                        self.callbacks.put("wrong_match_confirm", event_id, fixture),
                    ),
                    button("Cancel", self.callbacks.put("cancel")),
                ]
            ],
        )

    def on_cancel(self, chat_id: int, message_id: int | None) -> None:
        self.reply(chat_id, "Cancelled. Nothing was flagged.", message_id=message_id)

    def on_wrong_match_confirm(
        self, chat_id: int, message_id: int | None, event_id: str, fixture: str
    ) -> None:
        removed = self._verifier().flag_fixture(event_id, True, f"chat {chat_id}", fixture)
        self.store.add_report(chat_id, event_id, "*", "*", "wrong_match")
        self._count("wrong_matches")
        self.reply(
            chat_id,
            f"⚠️ <b>{render.e(fixture)}</b> is flagged as a wrong match. It no longer appears as "
            f"arbitrage or EV ({removed} stored signals removed), and no alerts will be sent "
            "for it. /flags to undo.",
            message_id=message_id,
        )

    def on_restore_match(
        self, chat_id: int, message_id: int | None, event_id: str, fixture: str
    ) -> None:
        self._verifier().flag_fixture(event_id, False, f"chat {chat_id}", fixture)
        self.api.send(
            chat_id,
            f"↩ <b>{render.e(fixture)}</b> is restored. Its signals return as its prices are "
            "next recomputed.",
        )

    def cmd_flags(self, chat_id: int, args: list[str], user: str | None) -> None:
        wrong = self._verifier().flagged_fixtures()
        if wrong:
            names = self._fixture_names()
            lines = ["⚠️ <b>Fixtures flagged as wrong matches</b>", ""]
            keyboard = []
            for n, (event_id, check) in enumerate(list(wrong.items())[:20], start=1):
                fixture = self._verifier()._fixtures.get(event_id, {}).get("fixture") or names.get(
                    event_id, event_id[:8]
                )
                lines.append(f"{n}. {render.e(fixture)} · {render.e(check.explanation)}")
                keyboard.append(
                    [
                        button(
                            f"↩ Restore {n}",
                            self.callbacks.put("restore_match", event_id, str(fixture)),
                        )
                    ]
                )
            self.api.send(chat_id, "\n".join(lines), keyboard)
        rows = self.store.flag_rows()[:20]
        if not rows:
            self.api.send(
                chat_id, "No legs are flagged." if not wrong else "No single legs are flagged."
            )
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
            # Price history and EV over time are charts: the event page has them.
            keyboard.append(
                [
                    url_button(f"📈 {n} ↗", self.event_url(row["eventId"]))
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
            keyboard.append(
                [
                    button(
                        f"⚠️ {n}",
                        self.callbacks.put("wrong_match", row["eventId"], row["fixture"]),
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
            [url_button(f"{name} ↗", self.event_url(event_id))] for event_id, name in seen.items()
        ]
        self.api.send(
            chat_id,
            f"🔎 The matches of <code>{render.e(share_code)}</code>. Each opens its event page: "
            "prices, form, settled markets, what punters backed, and any surebet or EV on it.",
            keyboard[:40] or None,
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
        # Brief and punters fit a screen; the price charts, form tables and
        # settled markets are the dashboard's, linked rather than paged here.
        keyboard = [
            [
                button("📝 Brief", self.callbacks.put("dive_part", event_id, "brief")),
                button("🏁 Post-match", self.callbacks.put("dive_part", event_id, "result")),
                button("👥 Punters", self.callbacks.put("dive_part", event_id, "punters")),
            ],
            [url_button("📊 Prices, form, settled markets ↗", self.dive_url(event_id))],
        ]
        self.api.send(chat_id, render.dive_overview(dive, datetime.now(UTC)), keyboard)

    def dive_url(self, event_id: str) -> str:
        return self.event_url(event_id)

    def event_url(self, event_id: str) -> str:
        """The fixture's page on the dashboard: every chart and history lives there."""
        return event_url(self.dashboard, event_id)

    def on_dive_part(self, chat_id: int, message_id: int | None, event_id: str, part: str) -> None:
        dive = self.store.dive(event_id)
        if dive is None:
            self.api.send(chat_id, "This deep dive is no longer published.")
            return
        text = {
            "brief": lambda: render.dive_note(dive, "brief"),
            "result": lambda: render.dive_note(dive, "result"),
            "punters": lambda: render.dive_punters(dive),
        }[part]()
        self.api.send(chat_id, text, [[url_button("Full deep dive ↗", self.dive_url(event_id))]])

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
                if err.code is None and "timed out" in err.description:
                    # A long poll outliving the network's patience: normal, not an error.
                    continue
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

    # --- the wallet -------------------------------------------------------------------------

    def _signed(self, chat_id: int) -> bool:
        return chat_id == self.owner or bool(self.store.balances(chat_id, "real"))

    def cmd_balance(self, chat_id: int, args: list[str], user: str | None) -> None:
        mode = "real"
        if args and args[0].lower() == "paper":
            mode, args = "paper", args[1:]
        if not args:
            real = self.store.balances(chat_id, "real")
            paper = self.store.balances(chat_id, "paper")
            lines = ["💼 <b>Balances</b>", ""]
            lines.append(
                "Real: "
                + (
                    " · ".join(
                        f"{render.e(b)} <b>{render.money(a)}</b>" for b, a in sorted(real.items())
                    )
                    if real
                    else "none set"
                )
            )
            if paper:
                lines.append(
                    "Paper: "
                    + " · ".join(
                        f"{render.e(b)} {render.money(a)}" for b, a in sorted(paper.items())
                    )
                )
            lines += [
                "",
                "Set one: <code>/balance msport 50000</code> · paper: "
                "<code>/balance paper msport 100000</code>",
                f"Books: {', '.join(BOOKS)}",
            ]
            self.api.send(chat_id, "\n".join(lines))
            return
        if len(args) != 2 or args[0].lower() not in BOOKS or parse_float(args[1]) is None:
            self.api.send(
                chat_id,
                f"Use <code>/balance &lt;book&gt; &lt;amount&gt;</code>, e.g. "
                f"<code>/balance msport 50000</code>. Books: {', '.join(BOOKS)}",
            )
            return
        book, amount = args[0].lower(), parse_float(args[1]) or 0.0
        if amount < 0:
            self.api.send(chat_id, "A balance cannot be negative.")
            return
        if self.store.subscriber(chat_id) is None:
            self.store.subscribe(chat_id, user)
            self.on_subscribers_changed()
        self.store.set_balance(chat_id, mode, book, amount)
        self._count("balances_set")
        word = "Paper balance" if mode == "paper" else "Balance"
        self.api.send(
            chat_id,
            f"✅ {word} at <b>{render.e(book)}</b> set to <b>{render.money(amount)}</b>. "
            "Alerts are now sized to your balances; /wallet shows them.",
        )

    def cmd_wallet(self, chat_id: int, args: list[str], user: str | None) -> None:
        mode = "paper" if args and args[0].lower() == "paper" else "real"
        balances = self.store.balances(chat_id, mode)
        bets = self.store.bets(chat_id, mode)
        if mode == "real" and not balances and not bets:
            self.api.send(
                chat_id,
                "No wallet yet. Set your cash at each book (<code>/balance msport 50000</code>) "
                "and record bets from alerts with ✅ Placed. Or try it on paper: 📝 Paper on any "
                "alert, then /wallet paper.",
            )
            return
        stats = summary(bets, balances)
        open_bets = [b for b in bets if b["status"] == "open"]
        self.api.send(
            chat_id,
            render.wallet(mode, balances, stats, open_bets),
            [[button("Settled bets", self.callbacks.put("bets", mode, "settled", 0))]]
            if stats["settled_bets"]
            else None,
        )

    def on_bets(
        self, chat_id: int, message_id: int | None, mode: str, status: str, start: int
    ) -> None:
        bets = [b for b in self.store.bets(chat_id, mode) if b["status"] == status]
        page = bets[start : start + 8]
        lines = [
            f"<b>{status.title()} {mode} bets</b> · {start + 1}–{start + len(page)} of {len(bets)}",
            "",
        ]
        lines += [render.bet_summary(b) for b in page]
        nav = []
        if start > 0:
            nav.append(button("◀ Prev", self.callbacks.put("bets", mode, status, start - 8)))
        if start + 8 < len(bets):
            nav.append(button("Next ▶", self.callbacks.put("bets", mode, status, start + 8)))
        self.reply(chat_id, render.clip("\n".join(lines)), [nav] if nav else None, message_id)

    def _sizing(self, chat_id: int, opp: Opportunity, mode: str) -> Any:
        balances = self.store.balances(chat_id, mode)
        if mode == "paper":
            # A practice wallet starts itself: PAPER_START at every book the bet needs.
            missing = {leg.book for leg in opp.legs} - set(balances)
            for book in missing:
                self.store.set_balance(chat_id, "paper", book, PAPER_START)
                balances[book] = PAPER_START
        cap = self._settings(chat_id).stake
        if opp.kind == "surebet":
            return size_surebet(
                [leg.odds for leg in opp.legs], [leg.book for leg in opp.legs], balances, cap
            )
        leg = opp.legs[0]
        return size_ev(opp.value, leg.odds, leg.book, balances, sum(balances.values()), cap)

    def on_placed(self, chat_id: int, message_id: int | None, opp: Opportunity, mode: str) -> None:
        if mode == "real" and not self._signed(chat_id):
            self.api.send(
                chat_id,
                "Recording real bets needs your balances first: "
                "<code>/balance msport 50000</code>. Or record it on paper with 📝 Paper.",
            )
            return
        sizing = self._sizing(chat_id, opp, mode)
        if sizing is None or sizing.total <= 0:
            self.api.send(
                chat_id,
                "Nothing to stake: no balance at "
                + ", ".join(render.e(b) for b in (sizing.short if sizing else []))
                + ". Set it with /balance, or send the stakes you placed: "
                "<code>/placed 4700 5000</code> after opening the card.",
            )
            return
        self._record(chat_id, opp, mode, sizing.stakes, "button")

    def _record(
        self, chat_id: int, opp: Opportunity, mode: str, stakes: list[float], source: str
    ) -> None:
        legs = [
            Leg(leg.book, str(leg.outcome_id), leg.outcome, leg.odds, stake)
            for leg, stake in zip(opp.legs, stakes, strict=True)
        ]
        now = datetime.now(UTC)
        bet_id = self.store.add_bet(bet_row(chat_id, mode, opp.kind, opp, legs, now, source))
        self.store.move_balances(chat_id, mode, {leg.book: -leg.stake for leg in legs})
        self.last_bet[chat_id] = bet_id
        self._count("bets_recorded")
        bet = self.store.bet(bet_id)
        assert bet is not None
        self.api.send(
            chat_id,
            f"{'📝' if mode == 'paper' else '✅'} <b>Recorded</b>"
            f"{' (paper)' if mode == 'paper' else ''}\n"
            + render.bet_summary(bet)
            + "\n\nDifferent amounts? <code>/placed "
            + " ".join(f"{leg.stake:.0f}" for leg in legs)
            + "</code> in that leg order. /wallet for the picture.",
        )

    def cmd_placed(self, chat_id: int, args: list[str], user: str | None) -> None:
        bet_id = self.last_bet.get(chat_id)
        bet = self.store.bet(bet_id) if bet_id else None
        if bet is None or bet["chat_id"] != chat_id:
            bet = self.store.last_bet(chat_id)
        card = self.viewing.get(chat_id)
        if bet is None and card is None:
            self.api.send(
                chat_id, "Nothing to correct yet: press ✅ Placed on an alert or card first."
            )
            return
        if bet is None or (card and not args):
            self.api.send(chat_id, "Send the stakes in leg order: <code>/placed 4700 5000</code>")
            return
        legs = bet["legs"]
        stakes = parse_stakes(args, len(legs))
        if stakes is None:
            self.api.send(
                chat_id,
                f"That bet has {len(legs)} legs: send {len(legs)} amounts in the order shown, "
                "e.g. <code>/placed 4700 5000</code>",
            )
            return
        if bet["status"] != "open":
            self.api.send(chat_id, "That bet has settled; its stakes cannot change.")
            return
        deltas: dict[str, float] = {}
        for leg, stake in zip(legs, stakes, strict=True):
            deltas[leg["book"]] = deltas.get(leg["book"], 0.0) + leg["stake"] - stake
            leg["stake"] = stake
        self.store.update_bet_legs(bet["bet_id"], legs)
        self.store.move_balances(chat_id, bet["mode"], deltas)
        bet["legs"] = legs
        self.api.send(chat_id, "✅ <b>Updated</b>\n" + render.bet_summary(bet))

    def on_odds(self, chat_id: int, message_id: int | None, opp: Opportunity) -> None:
        self.pending_odds[chat_id] = opp
        self.api.send(
            chat_id,
            "Send the odds the site shows, in leg order: <code>/odds "
            + " ".join(f"{leg.odds:.2f}" for leg in opp.legs)
            + "</code>",
        )

    def cmd_odds(self, chat_id: int, args: list[str], user: str | None) -> None:
        opp = self.pending_odds.get(chat_id)
        if opp is None:
            card = self.viewing.get(chat_id)
            if card is None:
                self.api.send(chat_id, "Press ✏️ Odds changed on an alert first, or open /surebets.")
                return
            opp = self._card_opportunity(card)
        numbers = [parse_float(a) for a in args]
        if len(numbers) != len(opp.legs) or any(n is None or n <= 1 for n in numbers):
            self.api.send(
                chat_id,
                f"Send {len(opp.legs)} odds above 1.00 in leg order: <code>/odds "
                + " ".join(f"{leg.odds:.2f}" for leg in opp.legs)
                + "</code>",
            )
            return
        odds = [float(n) for n in numbers]  # type: ignore[arg-type]
        for leg, price in zip(opp.legs, odds, strict=True):
            if abs(price - leg.odds) > 1e-9:
                self.store.add_report(
                    chat_id, opp.event_id, opp.market_id, leg.book, "odds_changed", price
                )
        legs = tuple(
            OppLeg(leg.outcome, leg.book, price, leg.url, leg.outcome_id)
            for leg, price in zip(opp.legs, odds, strict=True)
        )
        if opp.kind == "surebet":
            value = 1.0 / sum(1.0 / o for o in odds)
        else:
            value = (opp.probability or 0.0) * odds[0] - 1
        changed = Opportunity(**{**opp.__dict__, "legs": legs, "value": value})
        self.pending_odds.pop(chat_id, None)
        self._count("odds_reports")
        signed = self._signed(chat_id)
        sizing = self._sizing(chat_id, changed, "real") if signed else None
        now = datetime.now(UTC)
        verdict = ""
        if changed.kind == "surebet" and value <= 1:
            verdict = "\n\n⚠️ At these prices the return is below the stake: no longer a surebet."
        elif changed.kind == "ev" and value <= 0:
            verdict = "\n\n⚠️ At this price the edge is gone."
        keyboard = (
            [
                [
                    button(
                        "✅ Placed at these odds", self.callbacks.put("placed", changed, "real")
                    ),
                    button("📝 Paper", self.callbacks.put("placed", changed, "paper")),
                ]
            ]
            if not verdict
            else None
        )
        self.api.send(
            chat_id,
            "✏️ <b>Re-split at your odds</b>\n" + render.alert(changed, now, sizing) + verdict,
            keyboard,
        )

    def on_missing(self, chat_id: int, message_id: int | None, opp: Opportunity) -> None:
        if len(opp.legs) == 1:
            leg = opp.legs[0]
            self.on_flag(chat_id, None, opp.event_id, opp.market_id, leg.book, opp.fixture)
            self.store.add_report(chat_id, opp.event_id, opp.market_id, leg.book, "missing")
            return
        rows = [
            [
                button(
                    f"✕ {leg.outcome} @ {leg.book}",
                    self.callbacks.put("flag", opp.event_id, opp.market_id, leg.book, opp.fixture),
                )
            ]
            for leg in opp.legs
        ]
        self.api.send(
            chat_id,
            f"🚩 Which leg is not on the site?\n<b>{render.e(opp.fixture)}</b> · "
            f"{render.e(opp.market)}\n\nFlagging hides the market at that book for everyone.",
            rows,
        )

    # --- owner ------------------------------------------------------------------------------

    def cmd_admin(self, chat_id: int, args: list[str], user: str | None) -> None:
        if chat_id != self.owner:
            self.api.send(chat_id, "Unknown command. /help lists them.")
            return
        counts = self.store.counts()
        self.api.send(
            chat_id,
            "🔑 <b>Admin</b>\n\n"
            f"Subscribers: {counts['subscribers']} · signed (balances set): {counts['signed']}\n"
            f"Bets recorded: {counts['real_bets']} real, {counts['paper_bets']} paper · "
            f"reports: {counts['reports']}\n"
            f"Alerts since start: {self.stats.get('alerts_sent', 0)} · commands: "
            f"{self.stats.get('commands', 0)} · errors: {self.stats.get('command_errors', 0)}",
        )
