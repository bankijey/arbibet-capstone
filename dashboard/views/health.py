"""Pipeline health: what the Airflow UI used to show, readable from anywhere.

The runner records every job it runs (`ops.job_run`) and a heartbeat per
component (`ops.heartbeat`), and publishes both to Supabase. This page answers,
in order:

1. Is the pipeline alive?      heartbeats, and how long since each
2. Are signals fresh?          hot-loop latency: new price -> stored signal
3. Is anything failing?        failed runs, with the error
4. Is anything getting slow?   job durations over time
5. Is the Telegram bot working? alerts sent, commands, errors
"""

from __future__ import annotations

import json

import pandas as pd
import plotly.express as px
import streamlit as st
from dashboard.common import TIMEZONE, heartbeats, job_runs

st.title("Pipeline health")
st.caption(
    "Recorded by the pipeline runner on the pipeline machine and published here. "
    "If every heartbeat is old, the machine or the runner is off — the rest of the "
    "dashboard then shows its last published data."
)

# How old a heartbeat may be before the component counts as stale. The warm
# and cold loops only beat while working and when a cycle ends, so their bar is
# their schedule, not seconds.
STALE_AFTER = {
    "runner": pd.Timedelta(minutes=3),
    "listener": pd.Timedelta(minutes=3),
    "hot": pd.Timedelta(minutes=3),
    "warm": pd.Timedelta(minutes=45),
    "cold": pd.Timedelta(hours=26),
    "telegram": pd.Timedelta(minutes=3),
}
DESCRIPTION = {
    "runner": "the process itself",
    "listener": "hears new bookmaker payloads (Postgres NOTIFY)",
    "hot": "surebets and EV within seconds",
    "warm": "prices, slips, dbt, summaries, publishing — every 15 min",
    "cold": "match history, settlement, backups — daily 06:00",
    "telegram": "surebet and EV alerts, and commands",
}


def _detail(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return {}


def telegram_section(runs: pd.DataFrame, beats: pd.DataFrame, now: pd.Timestamp) -> None:
    beat = beats[beats.COMPONENT == "telegram"]
    if beat.empty:
        return
    st.subheader("Telegram bot")
    activity = runs[runs.JOB == "telegram.activity"]
    day = activity[pd.to_datetime(activity.STARTED_AT, utc=True) > now - pd.Timedelta(hours=24)]
    details = [_detail(v) for v in day.DETAIL]
    current = _detail(beat.DETAIL.iloc[0])
    lags = [d["alert_lag_median_s"] for d in details if d.get("alert_lag_median_s") is not None]
    m = st.columns(5)
    m[0].metric(
        "Subscribers", current.get("subscribers") if current.get("subscribers") is not None else "—"
    )
    m[1].metric(
        "Alerts, last 24 h",
        f"{sum(d.get('alerts_sent', 0) for d in details):,}",
        help="Surebet and EV alerts delivered, counted per subscriber.",
    )
    m[2].metric(
        "Alert lag",
        f"{pd.Series(lags).median():.1f} s" if lags else "—",
        help="Seconds from bronze storing the price to the alert being delivered.",
    )
    m[3].metric("Commands, last 24 h", f"{sum(d.get('commands', 0) for d in details):,}")
    errors = sum(
        d.get(k, 0)
        for d in details
        for k in ("command_errors", "send_errors", "alert_errors", "poll_errors")
    )
    m[4].metric("Errors, last 24 h", f"{errors:,}")
    if current.get("last_error"):
        st.caption(f"Last error: {current['last_error']}")


@st.fragment(run_every=30)
def health() -> None:
    now = pd.Timestamp.now(tz="UTC")
    beats = heartbeats()
    runs = job_runs(48)

    st.subheader("Components")
    if beats.empty:
        st.warning("No heartbeat has been published yet.")
    else:
        cols = st.columns(len(beats))
        for col, beat in zip(cols, beats.itertuples(), strict=False):
            age = now - pd.Timestamp(beat.BEAT_AT)
            stale = age > STALE_AFTER.get(beat.COMPONENT, pd.Timedelta(minutes=10))
            minutes = age.total_seconds() / 60
            ago = f"{age.total_seconds():.0f}s ago" if minutes < 2 else f"{minutes:.0f} min ago"
            state = "error" if beat.STATE == "error" else ("stale" if stale else beat.STATE)
            with col.container(border=True):
                marker = "🔴" if state in ("error", "stale") else "🟢"
                st.markdown(f"{marker} **{beat.COMPONENT}**")
                st.caption(f"{state} · {ago}")
                st.caption(DESCRIPTION.get(beat.COMPONENT, ""))
                detail = _detail(beat.DETAIL)
                if detail.get("error"):
                    st.caption(f"error: {detail['error']}")
                if beat.COMPONENT == "warm" and detail.get("last_cycle_failed"):
                    st.caption("last cycle failed: " + ", ".join(detail["last_cycle_failed"]))

    st.subheader("Signal freshness")
    hot = runs[runs.JOB == "hot.signals"].copy()
    hour = hot[pd.to_datetime(hot.STARTED_AT, utc=True) > now - pd.Timedelta(hours=1)]
    details = [_detail(v) for v in hour.DETAIL]
    medians = [d["latency_median_s"] for d in details if d.get("latency_median_s") is not None]
    maxima = [d["latency_max_s"] for d in details if d.get("latency_max_s") is not None]
    m = st.columns(5)
    m[0].metric(
        "Median latency, last hour",
        f"{pd.Series(medians).median():.1f} s" if medians else "—",
        help="Seconds from bronze storing a new payload to the signal being stored.",
    )
    m[1].metric("Worst latency, last hour", f"{max(maxima):.0f} s" if maxima else "—")
    m[2].metric("Fixtures recomputed, last hour", f"{sum(d.get('fixtures', 0) for d in details):,}")
    m[3].metric("Signal rows written, last hour", f"{int(hour.ROWS_WRITTEN.fillna(0).sum()):,}")
    notify = sum(d.get("wakes_notify", 0) for d in details)
    poll = sum(d.get("wakes_poll", 0) for d in details)
    m[4].metric(
        "Woken by NOTIFY",
        f"{notify / (notify + poll):.0%}" if notify + poll else "—",
        help="Share of hot-loop wakes triggered instantly rather than by the 15-second poll.",
    )
    if not hot.empty:
        series = pd.DataFrame(
            {
                "at": pd.to_datetime(hot.STARTED_AT, utc=True).dt.tz_convert(TIMEZONE),
                "median latency (s)": [_detail(v).get("latency_median_s") for v in hot.DETAIL],
            }
        ).dropna()
        if not series.empty:
            st.plotly_chart(
                px.line(
                    series.sort_values("at"), x="at", y="median latency (s)", height=240
                ).update_layout(margin={"t": 10, "b": 0, "l": 0, "r": 0}, xaxis_title=""),
                use_container_width=True,
            )

    telegram_section(runs, beats, now)

    st.subheader("Failures, last 48 hours")
    failed = runs[runs.STATUS == "failed"]
    if failed.empty:
        st.success("No failed runs.")
    else:
        st.dataframe(
            failed.assign(
                STARTED_AT=pd.to_datetime(failed.STARTED_AT, utc=True).dt.tz_convert(TIMEZONE)
            )[["STARTED_AT", "JOB", "ERROR"]],
            hide_index=True,
            use_container_width=True,
            column_config={
                "STARTED_AT": st.column_config.DatetimeColumn("When", format="D MMM HH:mm"),
                "JOB": "Job",
                "ERROR": "Error",
            },
        )

    st.subheader("Job durations")
    batch = runs[~runs.LOOP.isin(["hot", "telegram"]) & runs.FINISHED_AT.notna()].copy()
    if batch.empty:
        st.caption("No warm or cold runs recorded yet.")
    else:
        batch["seconds"] = (
            pd.to_datetime(batch.FINISHED_AT, utc=True) - pd.to_datetime(batch.STARTED_AT, utc=True)
        ).dt.total_seconds()
        batch["at"] = pd.to_datetime(batch.STARTED_AT, utc=True).dt.tz_convert(TIMEZONE)
        st.plotly_chart(
            px.scatter(
                batch, x="at", y="seconds", color="JOB", symbol="STATUS", height=320
            ).update_layout(margin={"t": 10, "b": 0, "l": 0, "r": 0}, xaxis_title=""),
            use_container_width=True,
        )
        latest = batch.sort_values("at").groupby("JOB").tail(1)
        sizes = [
            _detail(v).get("supabase_mb") for v in latest[latest.JOB.str.endswith("publish")].DETAIL
        ]
        if sizes and sizes[-1] is not None:
            st.caption(f"Serving database size at the last publish: {sizes[-1]} MB of 500 MB.")

    with st.expander("Every run, last 48 hours"):
        st.dataframe(
            runs.assign(
                STARTED_AT=pd.to_datetime(runs.STARTED_AT, utc=True).dt.tz_convert(TIMEZONE)
            )[["STARTED_AT", "JOB", "TRIGGER", "STATUS", "ROWS_WRITTEN", "ERROR"]],
            hide_index=True,
            use_container_width=True,
        )


health()
