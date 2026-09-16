"""The msport booking-code client.

Punters publish a slip, msport gives it a share code, and other punters copy
it. The list endpoint ranks those codes by how many people followed them; the
detail endpoint returns the legs. Both are read-only and public.

Two things about this source shape everything downstream:

**Legs disappear.** `bettableBetSlip` holds only legs that are still bettable,
so a leg vanishes the moment its match kicks off and there is no history
endpoint to recover it. Whatever is not captured before kickoff is gone.

**The list turns over every ten minutes** (msport's own `rules` text says so)
and is ranked by popularity descending, so depth matters more than frequency:
the top of the list is the stable, interesting part.

This module fetches and nothing else. Parsing lives in dbt, over the verbatim
payloads in `bronze_slip_payload`.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from typing import Any, NamedTuple

import httpx

_LIST_URL = "https://www.msport.com/api/ng/orders/euro-code/list"
_DETAIL_URL = "https://www.msport.com/api/ng/orders/real-sports/order/share/{code}"

SOURCE = "msport"

# msport rejects the default httpx agent; these mirror what the site sends.
_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "clientid": "WEB",
    "platform": "WEB",
    "channel": "share_code",
    "origin": "https://www.msport.com",
    "referer": "https://www.msport.com/ng/web/codelist/bookingcode",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    ),
}


class Settings(NamedTuple):
    """Fetch budget. This is someone else's production API and we have no
    agreement with them, so the defaults are deliberately unambitious: ten
    pages is 200 slips, and one second between requests is about two requests
    a minute sustained.
    """

    max_pages: int
    delay_seconds: float
    timeout_seconds: float

    @staticmethod
    def from_env() -> Settings:
        return Settings(
            max_pages=int(os.environ.get("SLIP_MAX_PAGES", "10")),
            delay_seconds=float(os.environ.get("SLIP_REQUEST_DELAY_SECONDS", "1.0")),
            timeout_seconds=float(os.environ.get("SLIP_TIMEOUT_SECONDS", "30")),
        )


class SlipRef(NamedTuple):
    """One entry from the ranked list, before its legs are fetched."""

    share_code: str
    list_id: str  # also the pagination cursor
    followed_times: int
    folds: int


def _entry(raw: dict[str, Any]) -> SlipRef:
    return SlipRef(
        share_code=str(raw["shareCode"]),
        list_id=str(raw.get("id", "")),
        followed_times=int(raw.get("followedTimes") or 0),
        folds=int(raw.get("folds") or 0),
    )


def list_codes(client: httpx.Client, settings: Settings) -> list[SlipRef]:
    """Walk the ranked list, most-followed first, up to `max_pages`.

    Paging is a cursor: `{"lastId": <id of the last entry seen>}`. Every other
    shape -- `pageNum`, `page`, `pageSize`, `currentPage` -- is silently
    IGNORED and returns page one again, which looks exactly like a short list
    rather than a mistake. That is why the cursor is asserted below: a page
    that repeats its predecessor means the contract changed, and fetching the
    same twenty codes ten times is worse than stopping.
    """
    found: list[SlipRef] = []
    seen: set[str] = set()
    cursor: str | None = None

    for _ in range(settings.max_pages):
        body: dict[str, Any] = {} if cursor is None else {"lastId": cursor}
        response = client.post(_LIST_URL, json=body, timeout=settings.timeout_seconds)
        response.raise_for_status()
        data = response.json().get("data") or {}
        entries = [_entry(raw) for raw in (data.get("codeList") or [])]
        if not entries:
            break

        fresh = [e for e in entries if e.share_code not in seen]
        if not fresh:
            # The cursor stopped advancing: paging broke rather than ended.
            break
        found.extend(fresh)
        seen.update(e.share_code for e in fresh)

        if not data.get("hasMore"):
            break
        cursor = entries[-1].list_id
        time.sleep(settings.delay_seconds)

    return found


def fetch_slip(client: httpx.Client, share_code: str, settings: Settings) -> dict[str, Any]:
    """The detail payload for one share code, verbatim."""
    response = client.get(
        _DETAIL_URL.format(code=share_code), timeout=settings.timeout_seconds
    )
    response.raise_for_status()
    return dict(response.json().get("data") or {})


# Leg fields that move with the MARKET rather than with the slip. A slip payload
# carries each leg's live odds and msport probability: of 40 slips fetched 30
# minutes apart, 40 had new probabilities and 39 new odds. Hashing them made
# every fetch a "new" payload -- ~9,600 bronze rows a day at half-hourly -- for
# price history `fact_odds_tick` already keeps. FINDINGS 13j.
_VOLATILE_OUTCOME_FIELDS = frozenset({"odds", "probability"})


def payload_hash(payload: dict[str, Any]) -> str:
    """Content hash over the canonical payload, leg prices excluded.

    `followedTimes` is inside it deliberately: a slip gaining followers is a
    change worth a row, because popularity over time is one of the questions
    this data exists to answer. Leg odds and probabilities are NOT: a slip
    whose only change is price drift updates its existing row in place (same
    key), so the stored payload still shows the prices of its latest write.
    """
    stable = dict(payload)
    stable["bettableBetSlip"] = [
        {
            **leg,
            "outcome": {
                k: v
                for k, v in (leg.get("outcome") or {}).items()
                if k not in _VOLATILE_OUTCOME_FIELDS
            },
        }
        if isinstance(leg, dict)
        else leg
        for leg in payload.get("bettableBetSlip") or []
    ]
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def open_client() -> httpx.Client:
    return httpx.Client(headers=_HEADERS)


def fetch_slips(
    client: httpx.Client, settings: Settings
) -> Iterator[tuple[SlipRef, dict[str, Any]]]:
    """Every slip in the fetch budget, paired with its detail payload.

    A detail fetch that fails is skipped rather than raised: the list is
    ranked, so a failure late in the fetch must not cost the popular slips
    fetched before it. The caller counts what came back.
    """
    for ref in list_codes(client, settings):
        time.sleep(settings.delay_seconds)
        try:
            yield ref, fetch_slip(client, ref.share_code, settings)
        except httpx.HTTPError:
            continue
