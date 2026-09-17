"""Is every book pricing the fixture we think it is?

The matcher (arbibet-matcher, upstream) occasionally merges two fixtures into
one event: Levski Sofia v Ludogorets and Etar v Chernomorets, same kick-off,
same country, one event id. Then livescorebet's Over 2.5 is one match and
msport's Under 2.5 is another, the arithmetic says "surebet", and a real bet
went on it. Nothing downstream could tell, because every book's payload was
keyed by the same event id.

The books can tell. Every payload names its own teams. So before a fixture's
prices are compared across books, each book's names are checked against the
fixture's:

1. NAMES (free, every recompute). Team names are normalised and compared by
   token overlap. Both sides sharing a distinctive token with the fixture's
   teams is a clear match; neither side sharing one is a clear mismatch.
2. MODEL (gpt-4o-mini, cents, once per event and book). Anything in between --
   transliterations, abbreviations, reserve sides, a women's team -- goes to
   the model with every book's names side by side, as JSON in and JSON out.
   The prompt gives it only the names, kick-off and competitions; it is asked
   one question and answers per book.

A book found on the wrong match is EXCLUDED from that fixture: not compared,
not stored, not alerted, not published, and its existing signals for the
fixture are removed. The verdict and its explanation are recorded in
`core.fixture_check`, which the dashboard and the bot both show, so a removed
signal is a visible decision rather than a silent gap.

Unverifiable (the model unreachable) is treated as not verified: the book is
left out until it can be checked, and the check is retried.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger("arbibet_capstone.verify")

MODEL = "gpt-4o-mini"
# Bumped whenever the prompt or the rules change: cached model verdicts from an
# older version are asked again rather than trusted.
PROMPT_VERSION = "v2"

# Tokens that say nothing about which club it is.
_NOISE = {
    "fc",
    "cf",
    "sc",
    "ac",
    "afc",
    "pfc",
    "sfc",
    "fk",
    "sk",
    "nk",
    "kf",
    "cd",
    "ca",
    "ud",
    "sd",
    "as",
    "us",
    "ss",
    "rc",
    "bk",
    "if",
    "ik",
    "ff",
    "club",
    "de",
    "da",
    "do",
    "del",
    "la",
    "le",
    "the",
    "team",
    "fc.",
    "vs",
    "v",
}
_YEAR = re.compile(r"^\d{4}$")


def normalise(name: str) -> set[str]:
    """Distinctive lower-case ASCII tokens of a team name."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    tokens = re.split(r"[^a-z0-9]+", text.lower())
    return {t for t in tokens if t and t not in _NOISE and not _YEAR.match(t) and len(t) > 1}


def similarity(a: str | None, b: str | None) -> float:
    """Share of the shorter name's distinctive tokens found in the other, 0..1."""
    if not a or not b:
        return 0.0
    ta, tb = normalise(a), normalise(b)
    if not ta or not tb:
        return 0.0
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    # Prefix matches too: "Ludogorets" against "Ludogorec", "Chernomorets" against "Chernom.".
    hits = sum(
        1 for t in shorter if any(u.startswith(t[:5]) or t.startswith(u[:5]) for u in longer)
    )
    return hits / len(shorter)


# --- team names from each book's payload ------------------------------------------------

_SPLIT = re.compile(r"\s+(?:-|vs\.?|v)\s+", re.I)


def _pair(text: Any) -> tuple[str, str] | None:
    if not isinstance(text, str):
        return None
    parts = _SPLIT.split(text.strip(), maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 and all(parts) else None


def _find(node: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    """First value under any of `keys`, searching a few levels down."""
    if depth > 4:
        return None
    if isinstance(node, dict):
        for key in keys:
            if key in node and node[key]:
                return node[key]
        for value in node.values():
            found = _find(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node[:5]:
            found = _find(value, keys, depth + 1)
            if found:
                return found
    return None


_HOME_KEYS = ("homeTeamName", "homeTeam", "home_team", "homeName", "home")
_AWAY_KEYS = ("awayTeamName", "awayTeam", "away_team", "awayName", "away")
_MATCH_KEYS = ("DS", "name", "eventName", "matchName", "title")


def team_names(bookmaker: str, payload: bytes | str | dict) -> tuple[str, str] | None:
    """(home, away) as the book names them, or None when the payload does not say."""
    try:
        data = payload if isinstance(payload, dict) else json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    home, away = _find(data, _HOME_KEYS), _find(data, _AWAY_KEYS)
    if isinstance(home, dict):
        home = home.get("name")
    if isinstance(away, dict):
        away = away.get("name")
    if isinstance(home, str) and isinstance(away, str) and home and away:
        return home.strip(), away.strip()
    pair = _pair(_find(data, _MATCH_KEYS))
    if pair:
        return pair
    log.debug("no team names found in a %s payload", bookmaker)
    return None


# --- verdicts ------------------------------------------------------------------------------

# Above: every distinctive token of the shorter name found in the other (a lone
# shared token like "Madrid" is not enough). At or below 0: none found.
CLEAR_MATCH = 0.75
CLEAR_MISMATCH = 0.0


@dataclass
class Check:
    bookmaker: str
    home: str | None
    away: str | None
    verdict: str  # ok | mismatch | unverified
    method: str  # names | model | none
    explanation: str
    score: float | None = None


@dataclass
class Result:
    checks: dict[str, Check] = field(default_factory=dict)

    @property
    def ok(self) -> set[str]:
        return {b for b, c in self.checks.items() if c.verdict == "ok"}

    @property
    def mismatched(self) -> dict[str, Check]:
        return {b: c for b, c in self.checks.items() if c.verdict == "mismatch"}

    @property
    def unverified(self) -> dict[str, Check]:
        return {b: c for b, c in self.checks.items() if c.verdict == "unverified"}


def by_names(
    fixture_home: str | None, fixture_away: str | None, names: Mapping[str, tuple[str, str] | None]
) -> Result:
    """The free check. Books it cannot settle are left `unverified` for the model."""
    result = Result()
    for book, pair in names.items():
        if pair is None:
            result.checks[book] = Check(
                book, None, None, "unverified", "none", "the payload names no teams"
            )
            continue
        home, away = pair
        s_home, s_away = similarity(fixture_home, home), similarity(fixture_away, away)
        score = (s_home + s_away) / 2
        if s_home >= CLEAR_MATCH and s_away >= CLEAR_MATCH:
            result.checks[book] = Check(
                book, home, away, "ok", "names", f"names agree ({home} v {away})", score
            )
        elif s_home <= CLEAR_MISMATCH and s_away <= CLEAR_MISMATCH:
            result.checks[book] = Check(
                book,
                home,
                away,
                "mismatch",
                "names",
                f"{book} lists {home} v {away}, not {fixture_home} v {fixture_away}",
                score,
            )
        else:
            result.checks[book] = Check(
                book,
                home,
                away,
                "unverified",
                "names",
                "names partly agree; needs the model",
                score,
            )
    return result


_SYSTEM = """You check whether bookmakers are listing the same football match. \
You are given one fixture (home team, away team, competition, kick-off), the \
names the other bookmakers list for it, and for each bookmaker IN QUESTION the \
home and away team names it lists under that fixture's id. Every listing shares \
the fixture's kick-off time.

Clubs appear under many names for the same club: spellings, transliterations, \
abbreviations, prefixes and suffixes (FC, PFC, SC, 1919, Razgrad, Sofia), \
language variants (Liège/Luik/Lüttich, Cologne/Köln), sponsor or stadium names, \
district names, and recent rebrands (a club that changed its name). All of \
those are the SAME club: answer true. Answer false ONLY when the names plainly \
denote a different pair of clubs -- two other teams entirely -- or a reserve, \
second, youth, under-21/23 or women's side where the fixture is the senior side \
(or the reverse). If you are unsure whether a name is an alias, answer true. \
Answer with JSON only: {"books": {"<bookmaker>": {"same_match": true|false, \
"reason": "<one short sentence>"}}}. Use only the names given."""


def by_model(
    client: Any,
    fixture: Mapping[str, Any],
    names: Mapping[str, tuple[str, str]],
    model: str = MODEL,
    context: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Check]:
    """Ask the model about the books in `names`, showing it the other books' names
    (`context`) as well. Raises on failure: the caller decides."""
    lines = [
        f"Fixture: {fixture.get('home')} v {fixture.get('away')}; competition "
        f"{fixture.get('tournament') or 'unknown'}; kick-off {fixture.get('kickoff')}.",
    ]
    if context:
        lines.append("Other bookmakers list it as:")
        lines += [f"- {book}: {home} v {away}" for book, (home, away) in context.items()]
    lines.append("Bookmakers in question:")
    for book, (home, away) in names.items():
        lines.append(f"- {book}: {home} v {away}")
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "\n".join(lines)},
        ],
        response_format={"type": "json_object"},
        max_tokens=400,
        temperature=0,
    )
    answer = json.loads(response.choices[0].message.content or "{}")
    books = answer.get("books") or {}
    checks: dict[str, Check] = {}
    for book, (home, away) in names.items():
        found = books.get(book)
        if not isinstance(found, dict) or "same_match" not in found:
            checks[book] = Check(
                book, home, away, "unverified", "model", "the model gave no verdict"
            )
            continue
        same = bool(found["same_match"])
        reason = str(found.get("reason") or "").strip()[:300]
        checks[book] = Check(
            book,
            home,
            away,
            "ok" if same else "mismatch",
            "model",
            reason
            or (
                "the model judged it the same match"
                if same
                else "the model judged it a different match"
            ),
        )
    return checks


def verify(
    fixture: Mapping[str, Any],
    payloads: Mapping[str, Any],
    client: Any | None,
    known: Mapping[str, Check] | None = None,
    model: str = MODEL,
) -> Result:
    """The whole check for one fixture.

    `payloads` maps bookmaker -> raw payload (bytes/str/dict). `known` holds
    earlier verdicts by bookmaker, reused when the book still names the same
    teams -- so the model is asked once per event and book, not per price.
    `client` None means no model: ambiguous books stay unverified.
    """
    # The payload's own names first; failing that, what the matcher recorded
    # for this book's listing (fixture["book_names"]). A book that names its
    # teams nowhere cannot be checked and is not excluded for it: that is the
    # pre-check behaviour, kept visible as method "none".
    from_matcher = fixture.get("book_names") or {}
    names = {
        book: team_names(book, raw) or from_matcher.get(book) for book, raw in payloads.items()
    }
    result = by_names(fixture.get("home"), fixture.get("away"), names)
    for book, check in result.checks.items():
        if check.home is None and check.verdict == "unverified":
            result.checks[book] = Check(
                book, None, None, "ok", "none", "no team names to check", None
            )
    for book, check in list(result.checks.items()):
        prior = (known or {}).get(book)
        if (
            prior is not None
            and prior.verdict in ("ok", "mismatch")
            and (prior.home, prior.away) == (check.home, check.away)
        ):
            result.checks[book] = prior
    pending = {
        b: (c.home, c.away)
        for b, c in result.checks.items()
        if c.verdict == "unverified" and c.home and c.away
    }
    if pending and client is not None:
        context = {
            b: (c.home, c.away)
            for b, c in result.checks.items()
            if b not in pending and c.home and c.away
        }
        try:
            result.checks.update(
                by_model(client, fixture, pending, model, context)  # type: ignore[arg-type]
            )
        except Exception as err:
            log.warning("model check failed for %s: %s", fixture.get("home"), err)
    return consensus(result)


def consensus(result: Result) -> Result:
    """When NO book matches the fixture's label but every book agrees with every
    other, the prices are comparable and it is the label that is stale (a
    renamed club, an odd spelling in the projection). If even one book matches
    the label, the label stands and the books that disagree with it stay
    excluded -- that is exactly the merged-event case."""
    named = {b: c for b, c in result.checks.items() if c.home and c.away}
    if len(named) < 2 or any(c.verdict == "ok" for c in named.values()):
        return result
    checks = list(named.values())
    first = checks[0]
    if all(
        similarity(first.home, c.home) >= CLEAR_MATCH
        and similarity(first.away, c.away) >= CLEAR_MATCH
        for c in checks[1:]
    ):
        for book, check in named.items():
            result.checks[book] = Check(
                book,
                check.home,
                check.away,
                "ok",
                "consensus",
                f"all {len(named)} books list {check.home} v {check.away}; "
                "the fixture label differs",
                check.score,
            )
    return result


def needs_model(result: Result) -> bool:
    return bool(result.unverified)


def describe(fixture_name: str, checks: Iterable[Check]) -> str:
    """One line per mismatched book, for a log or a message."""
    return (
        "; ".join(f"{c.bookmaker}: {c.explanation}" for c in checks if c.verdict == "mismatch")
        or f"{fixture_name}: every book agrees"
    )


def row(event_id: str, check: Check, at: datetime, model: str | None) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "bookmaker_name": check.bookmaker,
        "checked_at": at,
        "verdict": check.verdict,
        "book_home": check.home,
        "book_away": check.away,
        "method": check.method,
        "explanation": check.explanation,
        "model": f"{model}#{PROMPT_VERSION}" if check.method == "model" else None,
    }


def mismatched_books(warehouse: Any) -> dict[str, set[str]]:
    """event_id -> books excluded from it, for batch jobs that replay payload history."""
    frame = warehouse.query(
        "SELECT event_id, bookmaker_name FROM core.fixture_check WHERE verdict = 'mismatch'"
    )
    out: dict[str, set[str]] = {}
    for e, b in zip(frame.EVENT_ID, frame.BOOKMAKER_NAME, strict=True):
        out.setdefault(str(e), set()).add(str(b))
    return out
