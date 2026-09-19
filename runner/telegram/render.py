"""Telegram messages from alerts and serving documents. Pure, so it is tested.

HTML parse mode: only `<`, `>` and `&` need escaping, where MarkdownV2 would
need a dozen characters escaped in every team name. Every message stays under
Telegram's 4,096-character limit (`clip`); lists longer than a message are
paginated by the caller rather than cut.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from runner.telegram.model import Opportunity, split_stake

BERLIN = ZoneInfo("Europe/Berlin")
LIMIT = 4096
SAFE = 3900
SPARK = "▁▂▃▄▅▆▇█"


def e(value: Any) -> str:
    return escape("" if value is None else str(value), quote=False)


def clip(text: str, limit: int = SAFE) -> str:
    """At most `limit` characters, cut at a line break so no HTML tag is left open."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 2]
    if "\n" in cut:
        cut = cut[: cut.rfind("\n")]
    return cut + "\n…"


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def when(value: Any) -> str:
    """A timestamp in platform time: 'Thu 17 Sep 21:00'."""
    stamp = _dt(value)
    return "—" if stamp is None else stamp.astimezone(BERLIN).strftime("%a %d %b %H:%M")


def until(value: Any, now: datetime) -> str:
    stamp = _dt(value)
    if stamp is None:
        return ""
    minutes = (stamp - now).total_seconds() / 60
    if minutes < 0:
        return "started"
    if minutes < 90:
        return f"in {minutes:.0f} min"
    if minutes < 48 * 60:
        return f"in {minutes / 60:.0f} h"
    return f"in {minutes / 1440:.0f} d"


def link(url: str | None, label: Any) -> str:
    return f'<a href="{escape(url, quote=True)}">{e(label)}</a>' if url else e(label)


def money(value: float) -> str:
    return f"{value:,.2f}"


def pct(value: float) -> str:
    return f"{value:+.2%}"


def spark(values: Sequence[float | None]) -> str:
    clean = [v for v in values if v is not None]
    if not clean:
        return ""
    low, high = min(clean), max(clean)
    if high - low < 1e-12:
        return SPARK[3] * len(clean)
    return "".join(SPARK[round((v - low) / (high - low) * (len(SPARK) - 1))] for v in clean)


def reduce(values: Sequence[Any], points: int = 24) -> list[Any]:
    """At most `points` values, evenly spaced, first and last kept."""
    if len(values) <= points:
        return list(values)
    step = (len(values) - 1) / (points - 1)
    return [values[round(i * step)] for i in range(points)]


def markdown(text: str | None) -> str:
    """The AI summaries' **bold** as HTML, everything else escaped."""
    if not text:
        return ""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", e(text), flags=re.S)


def market_label(name: Any, line: Any) -> str:
    return f"{name} {line}" if line not in (None, "") else str(name)


def _upcoming(value: Any, now: datetime) -> bool:
    stamp = _dt(value)
    return stamp is not None and stamp > now


# --- alerts ---------------------------------------------------------------------------


def fair_odds(probability: float, source: str | None, url: str | None) -> str:
    """'fair odds 2.13', linked to the fixture at the book the probability came from.
    Without a link the book is named instead, so the number still has a source."""
    text = f"fair odds {1 / probability:.2f}"
    if url:
        return link(url, text)
    return f"{text} ({e(source)})" if source else text


NUDGE = (
    "<i>Stakes sized to your own balances at each book: set them with "
    "<code>/balance msport 50000</code>.</i>"
)


def sizing_lines(sizing: Any) -> list[str]:
    """What limited the suggested total, if anything."""
    lines = []
    if sizing.short:
        lines.append(
            f"<i>No balance at {', '.join(e(b) for b in sizing.short)}: set it with /balance.</i>"
        )
    elif sizing.limited_by:
        lines.append(f"<i>Total limited by your {e(sizing.limited_by)} balance.</i>")
    return lines


def alert(opp: Opportunity, now: datetime, sizing: Any = None, warnings: Sequence[str] = ()) -> str:
    """`sizing` is the subscriber's suggested stakes (runner.telegram.wallet.Sizing);
    None for a reader who has not set balances, who sees the prices and a nudge.
    `warnings` (model.match_warnings) go directly under the fixture: reasons to
    check the links show the same match before staking."""
    head = f"<b>{e(opp.fixture)}</b>" + (f" · {e(opp.tournament)}" if opp.tournament else "")
    if warnings:
        head += "\n" + "\n".join(f"⚠️ <b>Check the match:</b> {e(w)}." for w in warnings)
    kick = f"kick-off {when(opp.kickoff)} ({until(opp.kickoff, now)})"
    stakes = list(sizing.stakes) if sizing and sizing.total > 0 else None
    if opp.kind == "surebet":
        lines = [
            f"🟢 <b>SUREBET {opp.value:.4f}</b> · {pct(opp.value - 1)} guaranteed",
            head,
            f"{e(opp.market)} · {kick}",
            "",
        ]
        for i, leg in enumerate(opp.legs):
            share = f" · stake <b>{money(stakes[i])}</b>" if stakes else ""
            lines.append(
                f"• {e(leg.outcome)} @ <b>{leg.odds:.2f}</b> {link(leg.url, leg.book)}{share}"
            )
        if stakes:
            total = sum(stakes)
            returns = total * opp.value
            lines += [
                "",
                f"Stake {money(total)} returns <b>{money(returns)}</b> whatever wins "
                f"({money(returns - total)} profit).",
                *sizing_lines(sizing),
            ]
        elif sizing is None:
            lines += ["", NUDGE]
        else:
            lines += ["", *sizing_lines(sizing)]
        if opp.spread_seconds is not None:
            lines.append(
                f"<i>Legs priced {opp.spread_seconds}s apart. Check every price on the site "
                "before staking.</i>"
            )
        return clip("\n".join(lines))

    leg = opp.legs[0]
    lines = [
        f"📈 <b>EV {pct(opp.value)}</b> · {e(leg.outcome)} @ <b>{leg.odds:.2f}</b> "
        f"{link(leg.url, leg.book)}",
        head,
        f"{e(opp.market)} · {kick}",
    ]
    if opp.probability:
        lines.append(
            f"Probability {opp.probability:.1%} → "
            + fair_odds(opp.probability, opp.p_source, opp.p_source_url)
        )
    if stakes:
        lines.append(
            f"Suggested stake <b>{money(stakes[0])}</b> (a quarter of Kelly on your "
            f"{e(leg.book)} balance)"
        )
        lines += sizing_lines(sizing)
    elif sizing is None:
        lines.append(NUDGE)
    else:
        lines += sizing_lines(sizing)
    return clip("\n".join(lines))


def bet_summary(bet: dict[str, Any]) -> str:
    """One placed bet, as a line or two."""
    legs = bet["legs"]
    staked = sum(leg["stake"] for leg in legs)
    mark = {"open": "⏳", "settled": "✅" if (bet.get("profit") or 0) >= 0 else "❌"}.get(
        bet["status"], "➖"
    )
    if bet["status"] == "settled":
        outcome = f"{money(bet['profit'] or 0)} {'profit' if (bet['profit'] or 0) >= 0 else 'loss'}"
    elif bet["kind"] == "surebet" and legs:
        locked = min(leg["stake"] * leg["odds"] for leg in legs) - staked
        outcome = f"locks in {money(locked)}"
    else:
        outcome = "open"
    parts = ", ".join(
        f"{e(leg['outcome'])} @ {leg['odds']:.2f} {e(leg['book'])} {money(leg['stake'])}"
        for leg in legs
    )
    paper = " (paper)" if bet["mode"] == "paper" else ""
    if bet["status"] not in ("open", "settled"):
        outcome = e(bet["status"])
    if bet.get("note"):
        outcome += f" · <i>{e(bet['note'])}</i>"
    return (
        f"{mark} <b>{e(bet['fixture'])}</b> · {e(bet['market'])}{paper}\n"
        f"   {parts} → {outcome} · {when(bet['kickoff_at'])}"
    )


def wallet(
    mode: str, balances: dict[str, float], stats: dict[str, Any], open_bets: list[dict[str, Any]]
) -> str:
    title = "💼 <b>Wallet</b>" if mode == "real" else "📝 <b>Paper wallet</b>"
    lines = [title, ""]
    if balances:
        lines.append(
            "Balances: "
            + " · ".join(f"{e(b)} <b>{money(a)}</b>" for b, a in sorted(balances.items()))
        )
    else:
        lines.append("No balances set. <code>/balance msport 50000</code>")
    roi = f" · ROI {stats['roi']:+.1%}" if stats.get("roi") is not None else ""
    lines += [
        f"Equity <b>{money(stats['equity'])}</b> = cash {money(stats['cash'])} + "
        f"{money(stats['open_stake'])} in {stats['open_bets']} open bet"
        f"{'' if stats['open_bets'] == 1 else 's'}",
        f"Locked-in profit on open surebets: <b>{money(stats['locked_profit'])}</b>",
        f"Settled: {stats['settled_bets']} bets, {stats['settled_won']} won, "
        f"P&amp;L <b>{money(stats['profit'])}</b>{roi}",
    ]
    if open_bets:
        lines += ["", "<b>Open</b>"]
        lines += [bet_summary(b) for b in open_bets[:8]]
        if len(open_bets) > 8:
            lines.append(f"… and {len(open_bets) - 8} more")
    return clip("\n".join(lines))


# --- surebet cards --------------------------------------------------------------------


def surebet_cards(
    doc: dict[str, Any], flagged: set[tuple[str, str, str]], now: datetime
) -> list[dict[str, Any]]:
    """One card per upcoming market that has carried a surebet, as the dashboard shows them."""
    arb = doc.get("arbitrage") or {}
    all_legs = arb.get("legs") or []
    # A detection with any flagged leg is hidden whole, as on the dashboard.
    hidden = {
        leg["signalKey"]
        for leg in all_legs
        if (leg["eventId"], leg["marketId"], leg["book"]) in flagged
    }
    by_market: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for leg in all_legs:
        if leg["signalKey"] not in hidden:
            by_market.setdefault((leg["eventId"], leg["marketId"]), []).append(leg)
    cards = []
    for (event_id, market_id), legs in by_market.items():
        if not _upcoming(legs[0].get("kickoffAt"), now):
            continue
        best = max(legs, key=lambda leg: leg["arbitrage"])
        newest_key = max(legs, key=lambda leg: leg.get("detectedAt") or "")["signalKey"]
        newest = [leg for leg in legs if leg["signalKey"] == newest_key]
        last = ((arb.get("tracks") or {}).get(f"{event_id}|{market_id}") or {}).get("last")
        cards.append(
            {
                "eventId": event_id,
                "marketId": market_id,
                "fixture": best["fixture"],
                "market": market_label(best["market"], best.get("line")),
                "kickoffAt": best["kickoffAt"],
                "best": best["arbitrage"],
                "bestAt": best.get("detectedAt"),
                "detections": len({leg["signalKey"] for leg in legs}),
                "tracked": last is not None,
                "now": (last or {}).get("arbitrage"),
                "newestAt": newest[0].get("detectedAt"),
                "legs": [
                    {
                        "outcome": leg["outcome"],
                        "outcomeId": str(leg.get("outcomeId")),
                        "book": leg["book"],
                        "odds": leg["odds"],
                        "url": leg.get("url"),
                        "offered": leg.get("offered"),
                        "currentOdds": leg.get("currentOdds"),
                    }
                    for leg in sorted(newest, key=lambda leg: str(leg["outcome"]))
                ],
            }
        )
    cards.sort(key=lambda c: (c["kickoffAt"], -c["best"]))
    return cards


def _offered(item: dict[str, Any]) -> str:
    if item.get("offered") is False:
        return " · ⚠️ withdrawn"
    if item.get("offered") and item.get("currentOdds"):
        return f" · on site {item['currentOdds']:.2f}"
    return ""


def surebet_card(card: dict[str, Any], index: int, total: int, stake: float, now: datetime) -> str:
    split = split_stake([leg["odds"] for leg in card["legs"]], stake)
    if not card["tracked"]:
        now_text = "not tracked yet"
    elif card["now"] is None:
        now_text = "no price at the last check"
    else:
        now_text = f"{card['now']:.4f}"
    lines = [
        f"🟢 <b>Surebet {index + 1} of {total}</b>",
        f"<b>{e(card['fixture'])}</b>",
        f"{e(card['market'])} · kick-off {when(card['kickoffAt'])} "
        f"({until(card['kickoffAt'], now)})",
        "",
        f"Best detected <b>{card['best']:.4f}</b> ({when(card['bestAt'])}) · "
        f"{card['detections']} detection{'' if card['detections'] == 1 else 's'}",
        f"Now: <b>{now_text}</b>",
        "",
        f"Legs of the newest detection ({when(card['newestAt'])}):",
    ]
    for i, leg in enumerate(card["legs"]):
        share = f" · stake {money(split['stakes'][i])}" if split else ""
        lines.append(
            f"• {e(leg['outcome'])} @ <b>{leg['odds']:.2f}</b> {link(leg['url'], leg['book'])}"
            f"{share}{_offered(leg)}"
        )
    if split:
        lines.append(
            f"\nStake {money(stake)} returns {money(split['returns'])} "
            f"({pct(split['arbitrage'] - 1)}) at these prices."
        )
    return clip("\n".join(lines))


def stake_table(legs: Sequence[dict[str, Any]], odds: Sequence[float], stake: float) -> str:
    split = split_stake(list(odds), stake)
    if split is None:
        return "Every price must be above 1.00 and the stake above 0."
    verdict = "guaranteed profit" if split["arbitrage"] > 1 else "guaranteed LOSS"
    lines = [
        f"💰 <b>Stake split for {money(stake)}</b>",
        f"Arbitrage <b>{split['arbitrage']:.4f}</b> · {verdict} {money(split['profit'])} "
        f"({pct(split['arbitrage'] - 1)})",
        "",
    ]
    for i, price in enumerate(odds):
        name = f"{legs[i]['outcome']} @ {legs[i]['book']}" if i < len(legs) else f"leg {i + 1}"
        lines.append(
            f"• {e(name)}: odds {price:.2f} → stake <b>{money(split['stakes'][i])}</b>, "
            f"returns {money(split['stakes'][i] * price)}"
        )
    lines.append(
        "\nPrices moved? Send the site's odds in leg order: <code>/stake 100 2.10 1.95</code>"
    )
    return "\n".join(lines)


def track_summary(doc: dict[str, Any], event_id: str, market_id: str, title: str) -> str:
    track = ((doc.get("arbitrage") or {}).get("tracks") or {}).get(f"{event_id}|{market_id}")
    points = [p for p in (track or {}).get("points") or [] if p[1] is not None]
    if not points:
        return f"📈 <b>{e(title)}</b>\nNo arbitrage history tracked for this market yet."
    values = [p[1] for p in points]
    lines = [
        f"📈 <b>Arbitrage over time</b> · {e(title)}",
        f"<code>{spark(reduce(values))}</code>",
        f"First {values[0]:.4f} ({when(points[0][0])}) → last {values[-1]:.4f} "
        f"({when(points[-1][0])})",
        f"Range {min(values):.4f}–{max(values):.4f} · above 1.0 at {sum(v > 1 for v in values)} "
        f"of {len(values)} points shown ({track.get('total', len(values))} observed)",
    ]
    last = track.get("last") or {}
    if last.get("arbitrage") is None and last.get("at"):
        lines.append(f"At the last check ({when(last['at'])}) the market could not be priced.")
    return "\n".join(lines)


# --- EV -------------------------------------------------------------------------------


def ev_rows(
    doc: dict[str, Any], threshold: float, flagged: set[tuple[str, str, str]], now: datetime
) -> list[dict[str, Any]]:
    """Upcoming, fresh, unflagged EV at or above `threshold`, best first (the dashboard's)."""
    rows = [
        r
        for r in (doc.get("ev") or {}).get("rows") or []
        if r.get("isFresh") is True
        and (r.get("ev") or 0) >= threshold
        and _upcoming(r.get("kickoffAt"), now)
        and (r["eventId"], r["marketId"], r["book"]) not in flagged
    ]
    return sorted(rows, key=lambda r: -r["ev"])


def ev_page(
    rows: Sequence[dict[str, Any]], start: int, size: int, threshold: float, now: datetime
) -> str:
    total = len(rows)
    if not total:
        return f"📈 No upcoming EV of {threshold:.3f} or more right now."
    lines = [
        f"📈 <b>Upcoming EV ≥ {threshold:.3f}</b> · {start + 1}–{min(start + size, total)} "
        f"of {total}",
        "",
    ]
    for n, r in enumerate(rows[start : start + size], start=1):
        lines += [
            f"<b>{n}. EV {pct(r['ev'])}</b> · {e(r['outcome'])} @ <b>{r['odds']:.2f}</b> "
            f"{link(r.get('url'), r['book'])}{_offered(r)}",
            f"    {e(r['fixture'])} · {e(market_label(r['market'], r.get('line')))}",
            f"    p {r['impliedP']:.1%} → "
            f"{fair_odds(r['impliedP'], r.get('comparable'), r.get('comparableUrl'))} · kick-off "
            f"{when(r['kickoffAt'])} ({until(r['kickoffAt'], now)})",
        ]
    return clip("\n".join(lines))


def price_summary(doc: dict[str, Any], row: dict[str, Any]) -> str:
    key = f"{row['eventId']}|{row['marketId']}|{row['outcomeId']}"
    books = ((doc.get("ev") or {}).get("prices") or {}).get(key)
    title = f"{row['fixture']} · {market_label(row['market'], row.get('line'))} · {row['outcome']}"
    if not books:
        return f"💹 <b>{e(title)}</b>\nNo price history stored for this outcome yet."
    lines = [f"💹 <b>Prices</b> · {e(title)}", ""]
    for book, series in sorted(books.items()):
        if not series:
            continue
        odds = [p[1] for p in series]
        lines.append(
            f"<b>{e(book)}</b> <code>{spark(reduce(odds, 16))}</code> {odds[0]:.2f} → "
            f"<b>{odds[-1]:.2f}</b> (low {min(odds):.2f}, high {max(odds):.2f}), "
            f"{when(series[-1][0])}"
        )
    if row.get("impliedP"):
        lines.append(
            f"\nProbability {row['impliedP']:.1%} → "
            + fair_odds(row["impliedP"], row.get("comparable"), row.get("comparableUrl"))
        )
    return clip("\n".join(lines))


# --- slips ----------------------------------------------------------------------------

RESOLUTION = {"won": "✅", "lost": "❌", "void": "➖"}


def slip_lists(doc: dict[str, Any], now: datetime) -> tuple[list[dict], list[dict]]:
    """Popular slips with a verdict, most copied first: upcoming (a leg still to play), played."""
    cards = sorted(doc.get("cards") or [], key=lambda c: -(c.get("followedTimes") or 0))
    upcoming = [c for c in cards if _upcoming(c.get("lastKickoff"), now)]
    played = [c for c in cards if not _upcoming(c.get("lastKickoff"), now)]
    return upcoming, played


def slip(
    doc: dict[str, Any], card: dict[str, Any], index: int, total: int, label: str, now: datetime
) -> str:
    legs = (doc.get("legs") or {}).get(card["shareCode"]) or []
    to_play = sum(1 for leg in legs if _upcoming(leg.get("kickoffAt"), now))
    if to_play == len(legs):
        status = "upcoming"
    elif to_play == 0:
        status = "played"
    else:
        status = f"part-played, {to_play} of {len(legs)} to play"
    lines = [
        f"🎟 <b>{e(label)} slip {index + 1} of {total}</b> · <code>{e(card['shareCode'])}</code>",
        f"Copied <b>{int(card.get('followedTimes') or 0):,}×</b> · {len(legs)} legs · odds "
        f"<b>{card.get('combinedOdds') or 0:,.2f}</b> · {status}",
    ]
    if card.get("won") is not None or card.get("lost") is not None:
        lines.append(f"Legs won {int(card.get('won') or 0)}, lost {int(card.get('lost') or 0)}")
    lines.append("")
    for n, leg in enumerate(legs, start=1):
        mark = RESOLUTION.get(leg.get("resolution") or "", "⏳")
        match = f"{leg.get('home')} v {leg.get('away')}"
        history = ""
        if leg.get("historyMatches"):
            history = f" · history {int(leg.get('historyWins') or 0)}/{int(leg['historyMatches'])}"
        score = f" · {e(leg['score'])}" if leg.get("score") else ""
        lines.append(
            f"{mark} {n}. {link(leg.get('url'), match)} — {e(leg.get('market'))}: "
            f"<b>{e(leg.get('pick'))}</b> @ {leg.get('odds') or 0:.2f} · "
            f"{when(leg.get('kickoffAt'))}{score}{history}"
        )
    if card.get("summary"):
        lines += ["", "<b>AI verdict</b>", markdown(card["summary"])]
    return clip("\n".join(lines))


# --- deep dives -----------------------------------------------------------------------


def fixtures(doc: dict[str, Any], now: datetime, query: str | None = None) -> list[dict]:
    """Slipped fixtures: upcoming by popularity, or every match of a search, upcoming first."""
    popular = doc.get("popular") or []
    if query:
        q = query.lower().strip()
        found = [
            p
            for p in popular
            if q in str(p.get("fixture", "")).lower() or q in str(p.get("tournament", "")).lower()
        ]
        return sorted(
            found, key=lambda p: (not _upcoming(p.get("kickoffAt"), now), -(p.get("follows") or 0))
        )
    upcoming = [p for p in popular if _upcoming(p.get("kickoffAt"), now)]
    return sorted(upcoming, key=lambda p: -(p.get("follows") or 0))


def dive_overview(dive: dict[str, Any], now: datetime) -> str:
    punters = dive.get("punters") or {}
    lines = [
        f"🔎 <b>{e(dive['home'])} v {e(dive['away'])}</b>",
        f"{e(dive.get('tournament'))} · kick-off {when(dive['kickoffAt'])} "
        f"({until(dive['kickoffAt'], now)})",
        "",
        f"Brief: {'yes' if dive.get('brief') else 'not yet'} · post-match note: "
        f"{'yes' if dive.get('result') else 'not yet'}",
        f"Markets with price history: {len(dive.get('markets') or [])} · settled-market rows: "
        f"{len(dive.get('settled') or [])}",
    ]
    if punters:
        lines.append(
            f"Punters: {punters.get('slips', 0)} slips, {punters.get('copies', 0):,} copies"
        )
    lines.append("\nPick a section below.")
    return "\n".join(lines)


def dive_note(dive: dict[str, Any], which: str) -> str:
    part = dive.get(which)
    title = "Pre-match brief" if which == "brief" else "Post-match note"
    if not part:
        return f"📝 <b>{title}</b>\nNot written yet."
    return clip(
        f"📝 <b>{title}</b> · {e(dive['home'])} v {e(dive['away'])}\n\n"
        f"{markdown(part.get('summary'))}\n\n<i>{e(part.get('model'))}, "
        f"{when(part.get('generatedAt'))}</i>"
    )


def dive_prices(dive: dict[str, Any]) -> str:
    markets = dive.get("markets") or []
    if not markets:
        return "💹 <b>Prices</b>\nNo price history for this fixture."
    lines = [f"💹 <b>Prices</b> · {e(dive['home'])} v {e(dive['away'])}", ""]
    for market in markets:
        lines.append(
            f"<b>{e(market_label(market['name'], market.get('line')))}</b> · "
            f"{market.get('books')} books, {market.get('ticks')} price changes"
        )
        for outcome, books in (market.get("series") or {}).items():
            latest = {b: s[-1][1] for b, s in books.items() if s}
            if not latest:
                continue
            opened = {b: s[0][1] for b, s in books.items() if s}
            top = max(latest, key=lambda b: latest[b])
            moves = ", ".join(f"{e(b)} {opened[b]:.2f}→{latest[b]:.2f}" for b in sorted(latest))
            lines.append(f"  {e(outcome)}: best <b>{latest[top]:.2f}</b> {e(top)} · {moves}")
    return clip("\n".join(lines))


def _form_line(match: dict[str, Any]) -> str:
    venue = "H" if match.get("isHome") else "A"
    xg = ""
    if match.get("xg") is not None and match.get("xga") is not None:
        xg = f" · xG {match['xg']:.2f}–{match['xga']:.2f}"
    return (
        f"{match.get('result') or '?'} {match.get('goalsFor')}-{match.get('goalsAgainst')} "
        f"v {e(match.get('opponent'))} ({venue}){xg}"
    )


def dive_form(dive: dict[str, Any]) -> str:
    teams = dive.get("teams") or []
    if not teams:
        return "📊 <b>Form</b>\nNo match history for these teams."
    lines = ["📊 <b>Form</b> · most recent first", ""]
    for team in teams:
        matches = team.get("matches") or []
        results = "".join(m.get("result") or "?" for m in matches)
        scored = sum(m.get("goalsFor") or 0 for m in matches)
        conceded = sum(m.get("goalsAgainst") or 0 for m in matches)
        lines.append(
            f"<b>{e(team['name'])}</b> ({e(team.get('role'))}) <code>{results}</code> · goals "
            f"{scored}-{conceded} in {len(matches)}"
        )
        lines += [f"  {_form_line(m)}" for m in matches[:6]]
        lines.append("")
    return clip("\n".join(lines))


def dive_settled(dive: dict[str, Any]) -> str:
    rows = dive.get("settled") or []
    if not rows:
        return "✅ <b>Settled markets</b>\nNothing settled for these teams yet."
    rows = sorted(rows, key=lambda r: (-(r["landed"] / r["of"] if r.get("of") else 0), r["side"]))
    lines = ["✅ <b>Settled markets</b> · how often each landed in the sides' recent matches", ""]
    for r in rows[:20]:
        lines.append(
            f"{e(r['side'])} · {e(r['market'])} <b>{e(r['pick'])}</b> ({e(r.get('period'))}): "
            f"{r['landed']}/{r['of']}"
        )
    if len(rows) > 20:
        lines.append(f"… and {len(rows) - 20} more on the dashboard.")
    return clip("\n".join(lines))


def dive_punters(dive: dict[str, Any]) -> str:
    punters = dive.get("punters")
    if not punters:
        return "👥 <b>Punters</b>\nNo slips on this fixture."
    lines = [
        f"👥 <b>What punters backed</b> · {punters['slips']} slips, {punters['copies']:,} copies "
        f"(median {punters.get('medianCopies') or 0:,.0f})",
        "",
    ]
    for p in punters.get("picks") or []:
        mark = RESOLUTION.get(p.get("result") or "", "•")
        history = f" · history {int(p['wins'])}/{int(p['matches'])}" if p.get("matches") else ""
        lines.append(
            f"{mark} <b>{e(p['pick'])}</b> · {p['slips']} slips, {p['copies']:,} copies, median "
            f"odds {p.get('medianOdds') or 0:.2f}{history}"
        )
    return clip("\n".join(lines))


# --- health ---------------------------------------------------------------------------

STALE_AFTER = {
    "runner": 180,
    "listener": 180,
    "hot": 180,
    "telegram": 180,
    "warm": 45 * 60,
    "cold": 26 * 3600,
}


def health(
    beats: Iterable[dict[str, Any]],
    latency: dict[str, Any],
    failed: int,
    warm: dict[str, Any] | None,
    bot: dict[str, Any],
    now: datetime,
) -> str:
    lines = ["🩺 <b>Pipeline health</b>", ""]
    for b in beats:
        at = _dt(b.get("beat_at"))
        age = (now - at).total_seconds() if at else None
        bad = b.get("state") == "error" or age is None or age > STALE_AFTER.get(b["component"], 600)
        ago = "never" if age is None else (f"{age:.0f}s" if age < 120 else f"{age / 60:.0f} min")
        lines.append(
            f"{'🔴' if bad else '🟢'} <b>{e(b['component'])}</b> {e(b.get('state'))}, {ago} ago"
        )
    median, worst = latency.get("median"), latency.get("max")
    lines += [
        "",
        "Hot-loop latency, last hour: median "
        + ("—" if median is None else f"{median:.1f} s")
        + ", worst "
        + ("—" if worst is None else f"{worst:.0f} s"),
        f"Failed jobs, last 24 h: {failed}",
    ]
    if warm:
        lines.append(
            f"Last warm cycle: {warm.get('seconds', '?')} s, "
            f"finished {when(warm.get('finished_at'))}"
        )
    lines.append(
        f"Bot: {bot.get('subscribers', 0)} subscribers · {bot.get('alerts_sent', 0)} alerts and "
        f"{bot.get('commands', 0)} commands since the runner started"
    )
    return "\n".join(lines)
