"""Warehouse access shared by every dashboard page.

Still no `arbibet_capstone` import: the dashboard is a consumer of the
warehouse, not part of the pipeline, and Streamlit Community Cloud installs
from `requirements.txt` rather than the package. This module is dashboard-local
and deploys with it. It exists because the connection helpers were duplicated
across two pages, and a second copy of a credentials-and-timezone helper is a
second place for them to drift.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

import pandas as pd
import snowflake.connector
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

# The page refreshes when the WAREHOUSE changes, not on a clock. `CACHE_TTL`
# is only the backstop for the case where the watermark below cannot be read,
# so it is long: re-running every query on a schedule is exactly the cost this
# design exists to avoid.
CACHE_TTL = 3600
WATERMARK_TTL = 60

# Every timestamp in this platform is Europe/Berlin.
#
# Snowflake's session TIMEZONE defaults to America/Los_Angeles, and
# TIMESTAMP_TZ / TIMESTAMP_LTZ are RENDERED in it. Leaving it at the default
# corrupted no stored instant -- it printed every one of them nine hours early.
# Toulouse v Lille, an evening Ligue 1 fixture, showed an 11:45 kick-off on the
# deep dive; it is 20:45 in Berlin, which is what markets bronze meant, its own
# Postgres timezone being Europe/Berlin.
#
# Berlin rather than UTC because bronze is the upstream source of truth for
# fire_time and it speaks Berlin. Mixing zones is how a chart's x-axis and its
# kick-off marker end up disagreeing.
TIMEZONE = "Europe/Berlin"

# Each book in its own site's colour, read off the sites themselves: bet9ja's
# green, sportybet's red, livescorebet's orange, msport's yellow on black, and
# ilotbet's brighter green. bet9ja and ilotbet are both green, so ilotbet's
# lines are also dashed -- colour alone should never be the only difference.
BOOK_COLOURS = {
    "bet9ja": "#0D7B3C",
    "sportybet": "#E41827",
    "livescorebet": "#FF6B00",
    "msport": "#FFCA27",
    "ilotbet": "#1FCB6E",
}
BOOK_OUTLINE = {"msport": "#000000"}
BOOK_DASH = {"ilotbet": "dash"}


def setting(name: str, default: str | None = None) -> str:
    """`st.secrets` when deployed, environment when local."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        # No secrets.toml at all is the normal local case, not an error.
        pass
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"{name} is not set in st.secrets or the environment")
    return value


@st.cache_resource
def connection() -> Any:
    """One Snowflake session, reused across reruns and viewers.

    `client_session_keep_alive` is the whole reason this survives a night.
    Streamlit caches this object for the life of the process, and a Snowflake
    session's token expires after about four idle hours -- so a dashboard left
    open overnight woke up to `390114: Authentication token has expired` on
    every query. The keep-alive sends a heartbeat that renews the token instead
    of letting it lapse.
    """
    return snowflake.connector.connect(
        account=setting("SNOWFLAKE_ACCOUNT"),
        user=setting("SNOWFLAKE_USER"),
        password=setting("SNOWFLAKE_PASSWORD"),
        role=setting("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        warehouse=setting("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        database=setting("SNOWFLAKE_DATABASE", "ARBIBET_CAPSTONE"),
        schema=setting("SNOWFLAKE_SCHEMA", "CORE"),
        session_parameters={"TIMEZONE": TIMEZONE},
        client_session_keep_alive=True,
    )


# Snowflake's code for "the token is gone, authenticate again". Matched by
# number as well as text because the message wording is not a contract.
_EXPIRED_TOKEN = "390114"


# Snowflake stamps `last_altered` on every table it writes, and reading it is a
# METADATA query -- it does not resume the warehouse and does not bill compute.
# So this is the cheapest possible answer to "has anything changed since I last
# looked", which is the question the whole cache turns on.
#
# It moves when a pipeline task writes and sits still when nothing does; both
# were checked against a live consumer run. Every other query is keyed on its
# value, so a write anywhere in CORE or ANALYTICS invalidates the page and
# nothing else does. That is what lets the DAG run every thirty minutes without
# the dashboard either going stale or re-querying on a timer for no reason.
_WATERMARK = """
    SELECT max(last_altered) AS mark
    FROM ARBIBET_CAPSTONE.INFORMATION_SCHEMA.TABLES
    WHERE table_schema IN ('CORE', 'ANALYTICS')
"""


@st.cache_data(ttl=WATERMARK_TTL, show_spinner=False)
def watermark() -> str:
    """When the warehouse was last written, as a cache key.

    Failure is not fatal and must not be: a broken watermark should cost
    freshness, never the page. Returning a constant falls the cache back to
    `CACHE_TTL`, which is how this behaved before the watermark existed.
    """
    try:
        with connection().cursor() as cur:
            cur.execute(_WATERMARK)
            return str(cur.fetchone()[0])
    except Exception:
        return "unavailable"


def _fetch(sql: str) -> pd.DataFrame:
    with connection().cursor() as cur:
        cur.execute(sql)
        return pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _cached(sql: str, mark: str) -> pd.DataFrame:
    """Run a SELECT, cached until the warehouse is next written.

    `mark` is never read. It is in the signature because `st.cache_data` keys
    on the arguments, so threading the watermark through is what ties this
    cache to the warehouse's state rather than to a timer.

    Snowflake auto-suspends after five minutes, so an uncached page load would
    resume the warehouse on every visit and bill a minute of credit for a
    dashboard nobody is reading.

    A dead session is retried ONCE against a fresh connection. The keep-alive
    above should prevent expiry, but it cannot survive everything -- a laptop
    that slept, a dropped network, a session killed server-side -- and the
    failure mode without this is a permanently broken page that only a restart
    fixes. `st.cache_data` caches results, not exceptions, so a failed attempt
    leaves nothing stale behind.
    """
    try:
        return _fetch(sql)
    except snowflake.connector.errors.Error as err:
        if _EXPIRED_TOKEN not in f"{err}" and "token has expired" not in f"{err}".lower():
            raise
        connection.clear()
        return _fetch(sql)


def query(sql: str, *, stable: bool = False) -> pd.DataFrame:
    """Every page's read path.

    `stable=True` is for facts that cannot change any more -- a played match's
    history and prices. Those are cached against a constant rather than the
    warehouse watermark, so a half-hourly pipeline write does not throw away a
    deep dive that is exactly as true as it was an hour ago.
    """
    _start_warmer()
    if not stable:
        _RECENT[sql] = time.monotonic()
    return _cached(sql, "stable" if stable else watermark())


# --- background warming ----------------------------------------------------------
# A pipeline write every 30 minutes moves the watermark, and the next visitor to
# each page then paid for every query cold: measured at 10s for the slips page
# and 18s for the market signals page, almost all of it Snowflake. So a single
# background thread watches the watermark and, when it moves, re-runs every
# query a page has asked for in the last few hours -- and every registered
# derived computation -- against the NEW mark, before anyone asks. A visitor
# then lands on a warm cache.
_RECENT: dict[str, float] = {}
_RECENT_WINDOW = 6 * 3600
_WARMERS: dict[str, Callable[[str], object]] = {}


def register_warmer(name: str, fn: Callable[[str], object]) -> None:
    """A cached computation keyed on the watermark, to recompute on each write."""
    _WARMERS[name] = fn


def _read_watermark() -> str:
    with connection().cursor() as cur:
        cur.execute(_WATERMARK)
        return str(cur.fetchone()[0])


def _warm_loop() -> None:
    last: str | None = None
    while True:
        time.sleep(WATERMARK_TTL / 2)
        try:
            mark = _read_watermark()
            if last is not None and mark != last:
                cutoff = time.monotonic() - _RECENT_WINDOW
                for sql, used in list(_RECENT.items()):
                    if used >= cutoff:
                        _cached(sql, mark)
                for fn in list(_WARMERS.values()):
                    fn(mark)
            last = mark
        except Exception:
            # Warming is an optimisation. A failure costs the next visitor a
            # cold load, never the page.
            continue


@st.cache_resource(show_spinner=False)
def _start_warmer() -> threading.Thread:
    thread = threading.Thread(target=_warm_loop, name="cache-warmer", daemon=True)
    # Give the thread the script context of the run that started it, so the
    # cached functions it calls do not warn about a missing context.
    ctx = get_script_run_ctx()
    if ctx is not None:
        add_script_run_ctx(thread, ctx)
    thread.start()
    return thread


def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    """A dashboard WRITE, e.g. a viewer flagging a leg as not on the site.

    The watermark is cleared afterwards so the page reflects the write on the
    rerun that follows, rather than up to a minute later.
    """
    with connection().cursor() as cur:
        cur.execute(sql, params)
    watermark.clear()


def warehouse_written() -> str:
    """The watermark, for showing a reader why the page says what it says."""
    mark = watermark()
    if mark in ("unavailable", "None"):
        return "unknown"
    return str(mark)[:16].replace("T", " ")


def is_upcoming(kickoffs: pd.Series) -> pd.Series:
    """True where kick-off is still in the future, judged NOW.

    Deliberately not `kickoff_at > current_timestamp()` in SQL. Queries are
    cached until the warehouse is next written, so a SQL-side comparison would
    keep a fixture under "upcoming" for up to a whole DAG interval after it
    kicked off. Comparing here costs nothing and is right on every rerun.

    A missing kick-off is NOT upcoming: a fixture that has rolled out of
    `dim_fixture` is old, and a leg we cannot date must not be offered as a
    bet still to be placed.
    """
    now = pd.Timestamp.now(tz="UTC")
    return pd.to_datetime(kickoffs, utc=True).gt(now)


def kickoff(value: Any) -> str:
    """Kick-off in platform time, or a dash when the fixture has rolled away."""
    return "—" if pd.isna(value) else f"{value:%d %b %H:%M}"
