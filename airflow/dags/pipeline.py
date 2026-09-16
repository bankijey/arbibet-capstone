"""The capstone's two DAGs.

    SIGNALS  hourly -- the paths that must be fresh
      load_dims ── producer ─┬─ arb ─┬─ extract_ticks ── track_arbitrage ─┐
                             └─ ev ──┴─ live_state ───────────────────────┤
      ingest_slips ───────────────────────────────────────────────────────┴─ dbt_run
      dbt_run ── dbt_test ─┬─ brief_fixtures ─────┐
                           ├─ summarise_upcoming ─┼─ publish
                           └─ summarise_results ──┘

    HISTORY  daily at 06:00 -- expensive, and yesterday's answer is fine
      flatten ── settle ── dbt_run ── dbt_test ── summarise_played ── publish_archive

WHY TWO DAGS
------------
The signal path is worth running every half hour: an arbitrage on a fixture
that has not kicked off is only useful while it is still there. The history
path is not. `settle` writes 5.06M rows and takes seven minutes, `flatten`
scans 505 MB -- running those 48 times a day would burn Snowflake credits to
recompute a number that changes when a match finishes, which is once.

Splitting them is also what keeps warehouse writes minimal, which is what lets
the dashboard refresh often without the bill following it.

DECISION -- where pipeline tasks execute
----------------------------------------
Tasks run inside a custom Airflow image (`airflow/Dockerfile`: JDK 17 plus a
dedicated `/home/airflow/venv`) rather than shelling out to the host's
development venv.

The host venv is a Windows venv (`.venv/Scripts/python.exe`) and the Airflow
containers are Linux. A BashOperator runs inside the container, so it cannot
execute those binaries, and mounting the directory does not help -- they are
the wrong OS. Host execution was never actually on the table.

Consequences, all of which the tasks below depend on:

* Databases are reached at `host.docker.internal`, never `localhost`:
  markets bronze 5440, sources 5433, ingestor 5434.
* Credentials and `profiles.yml` are MOUNTED, never baked into the image.
* Project source is MOUNTED too, so a code change is not a rebuild. Only a
  dependency change requires `docker build`.
* The pipeline venv is separate from Airflow's own. Airflow pins a large
  dependency set and mixing pyspark, dbt and the Snowflake connector into it is
  a well-known way to break the scheduler. Every task is a BashOperator against
  `$PIPELINE_PYTHON` / `$PIPELINE_DBT`, so **this file imports nothing from the
  project** -- the scheduler parses it with Airflow's own interpreter, which has
  never heard of `arbibet_capstone`.

WHY THE LIVE PATH STILL LOOKS LIKE A BATCH
------------------------------------------
The producer and both consumers were built to make one pass and exit rather
than to loop, which is what lets them sit in a graph at all. Every thirty
minutes is the cadence a batch shape can carry honestly; `watch/signals.py` is
the same computation as a persistent process for when seconds matter.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

PROJECT = "/opt/project"

# JAVA_HOME and PYSPARK_PYTHON are set explicitly rather than inherited.
# `.env` holds HOST values (`C:\\jdk-17...`), and if the container was started
# with `--env-file .env` they reach the JVM and it never starts -- the failure
# is `Java gateway process exited before sending its port number`, a long way
# from its cause. Stating them per task makes the DAG correct however the
# container was launched.
SPARK_ENV = {
    "PYTHONPATH": f"{PROJECT}/src",
    "JAVA_HOME": "/usr/lib/jvm/java-17-openjdk-amd64",
    "PYSPARK_PYTHON": "/home/airflow/venv/bin/python",
}
PY_ENV = {"PYTHONPATH": f"{PROJECT}/src"}

log = logging.getLogger(__name__)


def _log_failure(context: dict[str, object]) -> None:
    """Failure notification.

    A logged error, not an email or a Slack message: this deployment has no
    SMTP server and no Slack connection, and a task configured to notify a
    channel that does not exist fails silently in a second place. Wiring a real
    channel is one Airflow connection plus a callback swap, and pretending it
    is done would be worse than saying it is not.
    """
    task = context.get("task_instance")
    log.error("PIPELINE TASK FAILED: %s", task)


default_args = {
    "owner": "arbibet",
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
    "on_failure_callback": _log_failure,
    # No email. See _log_failure.
    "email_on_failure": False,
}

# Shared by both DAGs: never backfill. Every task reads the CURRENT state of
# upstream bronze, not a partition of it, so a catch-up run would recompute
# today's answer once per missed interval and write the same rows each time.
_COMMON = {
    "start_date": datetime(2026, 9, 1),
    "catchup": False,
    "max_active_runs": 1,
    "default_args": default_args,
}


def _py(task_id: str, script: str, env: dict[str, str] = PY_ENV, **kwargs: object) -> BashOperator:
    return BashOperator(
        task_id=task_id,
        bash_command=f"$PIPELINE_PYTHON {PROJECT}/{script}",
        env=env,
        append_env=True,
        **kwargs,  # type: ignore[arg-type]
    )


def _dbt(task_id: str, command: str) -> BashOperator:
    return BashOperator(
        task_id=task_id,
        bash_command=f"cd {PROJECT}/dbt && $PIPELINE_DBT {command} --profiles-dir .",
        env=PY_ENV,
        append_env=True,
    )


with DAG(
    dag_id="arbibet_signals",
    description="Pre-kickoff arbitrage and EV, hourly.",
    # Hourly, not every 30 minutes. A run holds the warehouse for several
    # minutes and it bills per second while running plus a minute idle; at
    # half-hourly the runs plus dashboard checks kept COMPUTE_WH awake around
    # the clock, about 55 USD a day of trial credit on 15-16 Sep 2026.
    schedule="0 * * * *",
    tags=["capstone", "signals"],
    **_COMMON,  # type: ignore[arg-type]
) as signals:

    load_dims = _py("load_dims", "snowflake/load_dims.py 7 7")

    # Two hours back, six forward. The window is about whether bronze has
    # POLLED a fixture; the producer itself drops anything already kicked off,
    # which is a different question and the one that decides whether the bet
    # exists at all.
    producer = _py("producer", "producer/main.py 2 6")

    consume_arb = _py("consume_arb", "consumers/arb.py")
    consume_ev = _py("consume_ev", "consumers/ev.py")

    signals_dbt_run = _dbt("dbt_run", "run")
    signals_dbt_test = _dbt("dbt_test", "test")

    # Briefs the upcoming fixtures that actually produced a signal, and only
    # when the numbers moved enough to change what the brief would say.
    brief_fixtures = _py("brief_fixtures", "enrich/fixture_summary.py")

    # Slips moved here from the daily DAG. 60 verdicts per run. An upcoming slip is only worth a
    # verdict while it can still be placed, and a daily fetch met most of them
    # after kick-off: on 2026-09-15, 223 upcoming slips were waiting for one.
    # The fetch writes only slips whose content or copy count changed, so
    # running it every half hour does not mean rewriting 200 rows each time.
    ingest_slips = _py("ingest_slips", "slips/ingest.py")
    # The post-match note, every half hour rather than daily: a deep dive gets
    # its pre-match brief above and this after full time. Write-once, and a
    # match whose stats have not arrived is skipped until a later run.
    summarise_results = _py("summarise_results", "enrich/result_summary.py")
    summarise_upcoming = _py(
        "summarise_upcoming",
        "enrich/summarise.py",
        env={**PY_ENV, "SUMMARISE_SCOPE": "upcoming", "SUMMARISE_LIMIT": "60"},
    )

    # Every signals run, not daily: the surebet and EV cards show the LATEST
    # odds and where each tracked arbitrage stands, and a daily extraction left
    # "latest" up to a day old -- 4 of the 5 surebet markets signalled after
    # one day's extraction had no price history at all. Both are incremental
    # (cursor + seed), so each run replays only what bronze wrote since.
    # After the consumers, so a market signalled in this run is tracked in it.
    extract_ticks = _py("extract_ticks", "odds/ticks.py")
    track_arbitrage = _py("track_arbitrage", "odds/arbitrage_track.py")
    # Match status for slip fixtures, and whether each upcoming signal's legs
    # are still on the book's latest payload. After the consumers, so a signal
    # raised in this run is checked in it.
    live_state = _py("live_state", "odds/live_state.py")

    load_dims >> producer >> [consume_arb, consume_ev]
    [consume_arb, consume_ev] >> extract_ticks >> track_arbitrage
    [consume_arb, consume_ev] >> live_state
    [track_arbitrage, live_state, ingest_slips] >> signals_dbt_run
    signals_dbt_run >> signals_dbt_test >> [brief_fixtures, summarise_upcoming, summarise_results]

    # The web app's data: one snapshot per run, while the warehouse is still
    # awake from the steps above, so visitors never query Snowflake. all_done,
    # not all_success: a failed summary should not freeze the site on an older
    # snapshot of everything else.
    publish = _py("publish", "publish/snapshot.py", trigger_rule="all_done")
    [brief_fixtures, summarise_upcoming, summarise_results] >> publish


with DAG(
    dag_id="arbibet_history",
    description="Match history, settlement, slips and result summaries, daily.",
    schedule="0 6 * * *",
    tags=["capstone", "history"],
    **_COMMON,  # type: ignore[arg-type]
) as history:

    # Incremental. The full history is a 505 MB scan and roughly eighty
    # minutes; a daily run wants only what the ingestor fetched since
    # yesterday. The backfill is a one-off with this variable unset.
    #
    # No retry on either Spark task: they are the most expensive here and their
    # failures have been deterministic (a NOT NULL violation, a missing
    # dependency), not transient. Retrying doubles the cost of a certain
    # failure.
    flatten = _py(
        "flatten",
        "spark/flatten.py",
        env=SPARK_ENV,
        retries=0,
    )
    flatten.bash_command = (
        f"FLATTEN_SINCE_DAYS=3 $PIPELINE_PYTHON {PROJECT}/spark/flatten.py"
    )

    settle = _py("settle", "spark/settle.py", retries=0)

    history_dbt_run = _dbt("dbt_run", "run")
    history_dbt_test = _dbt("dbt_test", "test")

    # Played slips only; upcoming ones are the signals DAG's, hourly.
    summarise_played = _py(
        "summarise_played",
        "enrich/summarise.py",
        env={**PY_ENV, "SUMMARISE_SCOPE": "played", "SUMMARISE_LIMIT": "60"},
    )


    # Rewrites the last three archived days of deep dives, so post-match notes
    # written after a day was first archived still reach the site.
    publish_archive = _py(
        "publish_archive", "publish/snapshot.py --archive-recent", trigger_rule="all_done"
    )

    flatten >> settle >> history_dbt_run
    history_dbt_run >> history_dbt_test >> summarise_played >> publish_archive
