"""Serving-database access shared by every dashboard page.

The dashboard reads SUPABASE, never the warehouse. The warehouse is a DuckDB
file on the pipeline machine; the runner publishes what these pages show to
Supabase's free Postgres as JSON documents (runner/serve.py):

    serving.document 'signals'  headline numbers, arbitrage, EV, backtest
    serving.document 'slips'    deep-dive list, popular slips and their legs
    serving.dive                one fixture's deep dive
    serving.leg_flag            viewer flags (the one thing pages write)
    ops.job_run, ops.heartbeat  the pipeline's own record, for Pipeline health

So a page load is one or two small reads, and none of it costs anything: the
Snowflake version billed warehouse time for every visit and every refresh
check, which is what made it expensive.

Documents are converted back into the DataFrames, with the column names, the
pages were written against when they queried Snowflake -- so the pages' logic
and charts did not have to change with the move.

Still no `arbibet_capstone` import: Streamlit Community Cloud installs from
`requirements.txt`, not the package.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import psycopg
import streamlit as st

# Every timestamp is Europe/Berlin, as in the pipeline.
TIMEZONE = "Europe/Berlin"

# Each book in its own site's colour. bet9ja and ilotbet are both green, so
# ilotbet's lines are also dashed: colour alone is never the only difference.
BOOK_COLOURS = {
    "bet9ja": "#0D7B3C",
    "sportybet": "#E41827",
    "livescorebet": "#FF6B00",
    "msport": "#FFCA27",
    "ilotbet": "#1FCB6E",
}
BOOK_OUTLINE = {"msport": "#000000"}
BOOK_DASH = {"ilotbet": "dash"}

# How often a page checks whether the runner has published something newer.
# The check reads a two-row table; the documents themselves are re-read only
# when their `generated_at` moves.
VERSION_TTL = 20


def setting(name: str, default: str | None = None) -> str:
    """`st.secrets` when deployed, environment when local."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"{name} is not set in st.secrets or the environment")
    return value


@st.cache_resource(show_spinner=False)
def _connection() -> psycopg.Connection:
    # prepare_threshold=None: Supabase's pooler cannot keep prepared statements.
    return psycopg.connect(
        setting("SUPABASE_DB_URL"), autocommit=True, prepare_threshold=None, connect_timeout=15
    )


def _run(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Run a statement, reconnecting once if the cached connection went away."""
    for attempt in (1, 2):
        try:
            with _connection().cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []
        except psycopg.OperationalError:
            _connection.clear()
            if attempt == 2:
                raise
    return []


def unavailable(err: Exception) -> None:
    st.error(
        "The dashboard cannot reach its data right now. The pipeline publishes to a "
        "serving database; it may be restarting. Try again in a minute."
    )
    print(f"serving database error: {err}")
    st.stop()


@st.cache_data(ttl=VERSION_TTL, show_spinner=False)
def versions() -> dict[str, str]:
    return {name: str(at) for name, at in _run("SELECT name, generated_at FROM serving.document")}


@st.cache_data(show_spinner=False, max_entries=8)
def _document(name: str, version: str) -> dict[str, Any] | None:
    del version  # the cache key
    rows = _run("SELECT body FROM serving.document WHERE name = %s", (name,))
    return rows[0][0] if rows else None


def document(name: str) -> dict[str, Any]:
    """The newest published document, re-read only when the runner republishes it."""
    try:
        body = _document(name, versions().get(name, ""))
    except psycopg.Error as err:
        unavailable(err)
    if body is None:
        st.info("No data has been published yet. The pipeline publishes every 15 minutes.")
        st.stop()
    return body


@st.cache_data(ttl=300, show_spinner=False, max_entries=200)
def dive(event_id: str) -> dict[str, Any] | None:
    try:
        rows = _run("SELECT body FROM serving.dive WHERE event_id = %s", (event_id,))
    except psycopg.Error as err:
        unavailable(err)
    return rows[0][0] if rows else None


# --- flags -------------------------------------------------------------------------


@st.cache_data(ttl=10, show_spinner=False)
def flags() -> pd.DataFrame:
    rows = _run(
        "SELECT event_id, market_id, bookmaker_name, flagged_at "
        "FROM serving.leg_flag WHERE active"
    )
    frame = pd.DataFrame(rows, columns=["EVENT_ID", "MARKET_ID", "BOOKMAKER_NAME", "FLAGGED_AT"])
    frame["FLAGGED_AT"] = pd.to_datetime(frame.FLAGGED_AT, utc=True).dt.tz_convert(TIMEZONE)
    return frame


def set_flag(event_id: str, market_id: str, book: str, active: bool) -> None:
    try:
        _run(
            """
            INSERT INTO serving.leg_flag (event_id, market_id, bookmaker_name, active, flagged_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (event_id, market_id, bookmaker_name)
            DO UPDATE SET active = EXCLUDED.active, flagged_at = now()
            """,
            (event_id, market_id, book, active),
        )
    except psycopg.Error as err:
        unavailable(err)
    flags.clear()


# --- pipeline health -----------------------------------------------------------------


@st.cache_data(ttl=VERSION_TTL, show_spinner=False)
def heartbeats() -> pd.DataFrame:
    rows = _run("SELECT component, beat_at, state, detail FROM ops.heartbeat ORDER BY component")
    return pd.DataFrame(rows, columns=["COMPONENT", "BEAT_AT", "STATE", "DETAIL"])


@st.cache_data(ttl=VERSION_TTL, show_spinner=False)
def job_runs(hours: int) -> pd.DataFrame:
    rows = _run(
        "SELECT job, loop, trigger, started_at, finished_at, status, rows_written, detail, error "
        "FROM ops.job_run WHERE started_at > now() - make_interval(hours => %s) "
        "ORDER BY started_at DESC",
        (hours,),
    )
    return pd.DataFrame(
        rows,
        columns=[
            "JOB",
            "LOOP",
            "TRIGGER",
            "STARTED_AT",
            "FINISHED_AT",
            "STATUS",
            "ROWS_WRITTEN",
            "DETAIL",
            "ERROR",
        ],
    )


# --- documents as the frames the pages were written against ---------------------------


def at(value: Any) -> pd.Timestamp | None:
    """An ISO timestamp from a document, in platform time."""
    if value is None:
        return None
    return pd.Timestamp(value).tz_convert(TIMEZONE)


def frame(
    records: list[dict[str, Any]], columns: dict[str, str], times: tuple[str, ...] = ()
) -> pd.DataFrame:
    """Document records -> a DataFrame with the pages' UPPERCASE column names."""
    data = pd.DataFrame(records, columns=list(columns))
    data = data.rename(columns=columns)
    for column in times:
        data[column] = pd.to_datetime(data[column], utc=True).dt.tz_convert(TIMEZONE)
    return data


def points(series: list[list[Any]], *names: str) -> pd.DataFrame:
    """[[iso, v1, v2, ...], ...] -> a frame, first column a platform-time timestamp."""
    data = pd.DataFrame(series, columns=list(names))
    if not data.empty:
        data[names[0]] = pd.to_datetime(data[names[0]], utc=True).dt.tz_convert(TIMEZONE)
    return data


def published_at(value: str | None) -> str:
    stamp = at(value)
    return "unknown" if stamp is None else f"{stamp:%Y-%m-%d %H:%M}"


def is_upcoming(kickoffs: pd.Series) -> pd.Series:
    """True where kick-off is still in the future, judged NOW, not when published.

    Documents are published every few minutes; a match that kicked off since
    must not still be offered as a bet. A missing kick-off is not upcoming.
    """
    now = pd.Timestamp.now(tz="UTC")
    return pd.to_datetime(kickoffs, utc=True).gt(now)


def kickoff(value: Any) -> str:
    """Kick-off in platform time, or a dash."""
    if value is None or pd.isna(value):
        return "—"
    return f"{pd.Timestamp(value).tz_convert(TIMEZONE):%d %b %H:%M}"
