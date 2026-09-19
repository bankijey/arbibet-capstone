"""The local dashboard: pipeline health and the match-review queue.

Runs on the pipeline machine, not in the cloud, and reads the folder the
runner keeps for it (LOCAL_DIR, ./data/local bind-mounted into the container):

    status.json          heartbeats and recent runs, every 30 s
    fixture_checks.json  every book the check could not clear for a fixture --
                         candidates to review, plus what was decided
    decisions.json       written HERE; the runner applies it within a minute

Nothing touches DuckDB: the runner owns it, and a second process cannot open
the file. Run with:

    python -m streamlit run dashboard/local.py
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
LOCAL = Path(os.environ.get("LOCAL_DIR", str(ROOT / "data" / "local")))
if not LOCAL.is_absolute():
    LOCAL = ROOT / LOCAL
TIMEZONE = "Europe/Berlin"

st.set_page_config(page_title="Arbibet · local", page_icon="🛠", layout="wide")


def _read(name: str) -> dict | list | None:
    path = LOCAL / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _when(value: str | None) -> str:
    if not value:
        return "—"
    return f"{pd.Timestamp(value).tz_convert(TIMEZONE):%a %d %b %H:%M}"


def _detail(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value) if isinstance(value, str) else {}
    except ValueError:
        return {}


# --- health ---------------------------------------------------------------------------


def health() -> None:
    status = _read("status.json")
    if not status:
        st.warning(f"No status.json in {LOCAL}. Is the runner up, and LOCAL_DIR mounted?")
        return
    now = pd.Timestamp.now(tz="UTC")
    st.caption(f"Written {_when(status.get('written_at'))}")
    beats = pd.DataFrame(status["heartbeats"])
    stale = {"runner": 180, "listener": 180, "hot": 180, "telegram": 180, "warm": 45 * 60}
    cols = st.columns(max(len(beats), 1))
    for col, b in zip(cols, beats.itertuples(), strict=False):
        age = (now - pd.Timestamp(b.beat_at)).total_seconds()
        bad = b.state == "error" or age > stale.get(b.component, 26 * 3600)
        with col.container(border=True):
            st.markdown(f"{'🔴' if bad else '🟢'} **{b.component}**")
            st.caption(
                f"{b.state} · {age:.0f}s ago"
                if age < 120
                else f"{b.state} · {age / 60:.0f} min ago"
            )
            d = _detail(b.detail)
            if b.component == "hot":
                st.caption(
                    f"latency median {d.get('latency_median_s')} s · watched {d.get('watched')}"
                )
            if b.component == "warm":
                st.caption(
                    f"job {d.get('job') or 'idle'} · last cycle {d.get('last_cycle_seconds')} s"
                )
            if d.get("error") or d.get("last_error"):
                st.caption(f"error: {d.get('error') or d.get('last_error')}")
    runs = pd.DataFrame(status["runs"])
    if runs.empty:
        return
    runs["started_at"] = pd.to_datetime(runs.started_at, utc=True).dt.tz_convert(TIMEZONE)
    runs["finished_at"] = pd.to_datetime(runs.finished_at, utc=True).dt.tz_convert(TIMEZONE)
    runs["seconds"] = (runs.finished_at - runs.started_at).dt.total_seconds().round()
    failed = runs[runs.status == "failed"]
    if not failed.empty:
        st.error(f"{len(failed)} failed run(s) in the last {len(runs)}")
        st.dataframe(
            failed[["started_at", "job", "error"]], hide_index=True, use_container_width=True
        )
    st.dataframe(
        runs[["started_at", "job", "status", "seconds", "rows_written", "detail"]],
        hide_index=True,
        use_container_width=True,
        height=360,
    )


# --- match review -----------------------------------------------------------------------


def _decisions() -> list[dict]:
    return _read("decisions.json") or []


def _decide(event_id: str, book: str, verdict: str, note: str, row: dict) -> None:
    decisions = [d for d in _decisions() if not (d["eventId"] == event_id and d["book"] == book)]
    decisions.append(
        {
            "eventId": event_id,
            "book": book,
            "verdict": verdict,
            "note": note or None,
            "bookHome": row.get("bookHome"),
            "bookAway": row.get("bookAway"),
            "at": datetime.now(UTC).isoformat(),
        }
    )
    LOCAL.mkdir(parents=True, exist_ok=True)
    tmp = LOCAL / "decisions.tmp"
    tmp.write_text(json.dumps(decisions, indent=1), encoding="utf-8")
    os.replace(tmp, LOCAL / "decisions.json")


def review() -> None:
    data = _read("fixture_checks.json")
    if not data:
        st.info(f"No fixture_checks.json in {LOCAL} yet.")
        return
    st.caption(
        f"Written {_when(data.get('writtenAt'))}. Each row is a book whose payload names "
        "teams the check could not match to the fixture it is filed under. Nothing is "
        "excluded until you decide: **Exclude** removes the book from that fixture "
        "everywhere and drops its signals; **Clear** keeps it (an alias, a rebrand). "
        "Decisions reach the runner within a minute."
    )
    checks = pd.DataFrame(data["checks"])
    if checks.empty:
        st.success("Nothing to review.")
        return
    pending = {(d["eventId"], d["book"]): d for d in _decisions()}
    checks["decided"] = [
        pending.get((r.eventId, r.book), {}).get("verdict") for r in checks.itertuples()
    ]
    checks["kickoff"] = pd.to_datetime(checks.get("kickoffAt"), utc=True, errors="coerce")
    upcoming = checks.kickoff.isna() | (checks.kickoff > pd.Timestamp.now(tz="UTC"))
    queue = checks[(checks.verdict == "candidate") & checks.decided.isna() & upcoming]
    st.subheader(f"To review · {len(queue)}")
    for event_id, group in queue.sort_values("kickoff").groupby("eventId", sort=False):
        head = group.iloc[0]
        with st.container(border=True):
            st.markdown(
                f"**{head.get('fixture') or event_id}** · {head.get('tournament') or ''} · "
                f"kick-off {_when(head.get('kickoffAt'))}"
            )
            for _, r in group.iterrows():
                c1, c2, c3, c4 = st.columns([3, 3, 1, 1])
                c1.markdown(f"**{r.book}** lists *{r.bookHome} v {r.bookAway}*")
                c2.caption(f"{r.method}: {r.explanation}")
                note = c2.text_input(
                    "note",
                    key=f"n-{event_id}-{r.book}",
                    label_visibility="collapsed",
                    placeholder="note (optional)",
                )
                if c3.button("Exclude", key=f"x-{event_id}-{r.book}", type="primary"):
                    _decide(event_id, r.book, "mismatch", note, r.to_dict())
                    st.rerun()
                if c4.button("Clear", key=f"c-{event_id}-{r.book}"):
                    _decide(event_id, r.book, "cleared", note, r.to_dict())
                    st.rerun()

    st.subheader("Decided")
    st.caption("Book `*` is the whole fixture, flagged as a wrong match in Telegram or here.")
    decided = checks[checks.verdict.isin(["mismatch", "cleared"]) | checks.decided.notna()].copy()
    if decided.empty:
        st.caption("No decisions yet.")
    else:
        decided["verdict"] = decided.decided.fillna(decided.verdict)
        st.dataframe(
            decided[
                ["fixture", "kickoffAt", "book", "bookHome", "bookAway", "verdict", "explanation"]
            ],
            hide_index=True,
            use_container_width=True,
        )
        undo = st.selectbox(
            "Reverse a decision",
            ["—"] + [f"{r.book} · {r.fixture}" for r in decided.itertuples()],
        )
        if undo != "—" and st.button("Put it back in the queue"):
            book, fixture = undo.split(" · ", 1)
            r = decided[(decided.book == book) & (decided.fixture == fixture)].iloc[0]
            current = "cleared" if r.verdict == "mismatch" else "mismatch"
            # Flip: the runner applies whichever verdict is written last.
            _decide(r.eventId, book, current, "reversed on review", r.to_dict())
            st.rerun()

    with st.expander(f"Cleared automatically by names ({int((checks.verdict == 'ok').sum())})"):
        st.caption("Not shown: the file only carries what the check could not clear.")


st.title("Arbibet · local")
tab_health, tab_review = st.tabs(["Pipeline health", "Match review"])
with tab_health:
    health()
with tab_review:
    review()
