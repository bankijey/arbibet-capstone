"""Flatten api-football match payloads into `fact_team_match`.

154,088 nested JSON payloads, 505 MB, two rows out per finished fixture -- one
per team. This is the job Spark is here for; the dimension loads deliberately
are not.

Read path: the only index on `bronze_fixture_details` is
`(fixture_id, ingested_at DESC)`, so any filter that is not by fixture id is a
full scan. Acceptable exactly once, in a batch like this, and never in a query
path. The read is partitioned on the `id` primary key so the scan is parallel
rather than one long cursor.

Load path: Spark transforms, `write_pandas` loads. The Snowflake Spark
connector would mean version-matching jars against Spark 4.2 for an output that
fits in memory comfortably. Saying that plainly is better than implying Spark
wrote to the warehouse.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import pandas as pd
from pyspark.sql import Column, DataFrame, DataFrameReader, SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType,
    IntegerType,
    LongType,
    StringType,
    StructField,
    StructType,
)

from arbibet_capstone.env import load as load_env
from arbibet_capstone.env import require
from arbibet_capstone.warehouse import connect, merge_bulk

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("flatten")

# Only finished matches have a settleable result, and D30's four score
# containers are only meaningfully populated for these.
FINISHED = ("FT", "AET", "PEN")

# fact_team_match declares these NOT NULL. One fixture in 153,386 has neither a
# fulltime score nor a goals total -- finished, but with no result recorded --
# and that single row failed the whole batch with "NULL result in a
# non-nullable column" after eighty minutes of transformation. A finished match
# with no score is unusable for settlement or for form, so it is dropped; the
# count is logged, because silently filtering data is how a partial load starts
# looking like a complete one.
REQUIRED = (
    "fixture_id",
    "team_id",
    "team_name",
    "opponent_id",
    "opponent_name",
    "match_date",
    "goals_for",
    "goals_against",
    "result",
)

# A PARTIAL schema, on purpose. The payload also carries `events`, `lineups`
# and `players`; naming only what is read keeps the parse cheap and leaves the
# unread arrays visible as the post-capstone work they are.
_SCORE = StructType([StructField("home", IntegerType()), StructField("away", IntegerType())])
_TEAM = StructType([StructField("id", LongType()), StructField("name", StringType())])
_RESPONSE = StructType(
    [
        StructField(
            "fixture",
            StructType(
                [
                    StructField("id", LongType()),
                    StructField("date", StringType()),
                    StructField("status", StructType([StructField("short", StringType())])),
                ]
            ),
        ),
        StructField(
            "league",
            StructType(
                [
                    StructField("name", StringType()),
                    StructField("season", IntegerType()),
                    StructField("round", StringType()),
                ]
            ),
        ),
        StructField(
            "teams",
            StructType([StructField("home", _TEAM), StructField("away", _TEAM)]),
        ),
        StructField("goals", _SCORE),
        StructField(
            "score",
            StructType(
                [
                    StructField("halftime", _SCORE),
                    StructField("fulltime", _SCORE),
                    StructField("extratime", _SCORE),
                    StructField("penalty", _SCORE),
                ]
            ),
        ),
        StructField(
            "statistics",
            ArrayType(
                StructType(
                    [
                        StructField("team", _TEAM),
                        StructField(
                            "statistics",
                            ArrayType(
                                StructType(
                                    [
                                        StructField("type", StringType()),
                                        StructField("value", StringType()),
                                    ]
                                )
                            ),
                        ),
                    ]
                )
            ),
        ),
    ]
)
_PAYLOAD = StructType([StructField("response", ArrayType(_RESPONSE))])

# API-Football's statistic labels, to column names. Two encode traps silver's
# D16 recorded: `goals_prevented` is reported IDENTICALLY for both teams, so it
# is fixture-level and named to say so; `Passes accurate` is a count, not the
# percentage `Passes %` carries.
STAT_COLUMNS = {
    "Shots on Goal": "shots_on",
    "Shots off Goal": "shots_off",
    "Total Shots": "total_shots",
    "Blocked Shots": "blocked_shots",
    "Shots insidebox": "shots_inside",
    "Shots outsidebox": "shots_outside",
    "Fouls": "fouls",
    "Corner Kicks": "corners",
    "Offsides": "offsides",
    "Ball Possession": "possession",
    "Yellow Cards": "yellow",
    "Red Cards": "red",
    "Goalkeeper Saves": "saves",
    "Total passes": "passes_total",
    "Passes accurate": "passes_accurate",
    "Passes %": "passes_pct",
    "expected_goals": "xg",
    "goals_prevented": "goals_prevented_fixture",
}


def session() -> SparkSession:
    return (
        SparkSession.builder.appName("flatten")
        .master(os.environ.get("SPARK_MASTER", "local[*]"))
        # Fetched at session start rather than vendored: one Maven coordinate
        # is easier to audit than a jar of unknown provenance in the repo.
        .config("spark.jars.packages", "org.postgresql:postgresql:42.7.4")
        # The runner logs to a file; a redrawn progress bar is megabytes of noise.
        .config("spark.ui.showConsoleProgress", "false")
        .config("spark.sql.execution.arrow.pyspark.enabled", "true")
        .getOrCreate()
    )


def _jdbc(spark: SparkSession, url: str, user: str, password: str, table: str) -> DataFrameReader:
    return (
        spark.read.format("jdbc")
        .option("url", url)
        .option("user", user)
        .option("password", password)
        .option("driver", "org.postgresql.Driver")
        .option("dbtable", table)
    )


def _since_clause() -> str:
    """An optional `ingested_at` bound, for incremental runs.

    The full history is a 505 MB scan and roughly eighty minutes of parsing --
    right once, wrong daily. A scheduled run wants only what has arrived since
    the last one. The scan is unavoidable either way (the only index is
    `(fixture_id, ingested_at DESC)`), but the transfer and the JSON parse are
    the expensive parts and both shrink with the window.
    """
    days = os.environ.get("FLATTEN_SINCE_DAYS")
    if not days:
        return ""
    return f" WHERE ingested_at >= now() - interval '{int(days)} days'"


def read_payloads(spark: SparkSession, partitions: int = 8) -> DataFrame:
    """Every payload, read in parallel slices of the primary key.

    `FLATTEN_LIMIT` caps the read. The full set is 505 MB and takes a long
    time on one machine; proving the transformation on a few thousand rows
    first is cheaper than discovering a wrong column after half an hour.
    """
    parsed = urlparse(require("INGESTOR_DB_URL"))
    url = f"jdbc:postgresql://{parsed.hostname}:{parsed.port or 5432}{parsed.path}"
    user, password = parsed.username or "", parsed.password or ""

    lo, hi = (
        _jdbc(
            spark,
            url,
            user,
            password,
            "(SELECT min(id) lo, max(id) hi FROM bronze_fixture_details) b",
        )
        .load()
        .collect()[0]
    )

    # jsonb has no Spark type; cast to text and parse with an explicit schema,
    # which is faster than inferring one anyway.
    frame = (
        _jdbc(
            spark,
            url,
            user,
            password,
            "(SELECT id, fixture_id, ingested_at, payload::text AS payload "
            f"FROM bronze_fixture_details{_since_clause()}) t",
        )
        .option("partitionColumn", "id")
        .option("lowerBound", str(lo))
        .option("upperBound", str(hi))
        .option("numPartitions", str(partitions))
        .load()
    )
    limit = os.environ.get("FLATTEN_LIMIT")
    return frame.limit(int(limit)) if limit else frame


def latest_per_fixture(raw: DataFrame) -> DataFrame:
    """One payload per fixture: the most recently ingested.

    `bronze_fixture_details` is append-only, so a fixture re-fetched after the
    match has several rows. Flattening all of them yields more than two rows
    per fixture -- caught by 3,000 payloads producing 6,120 rows rather than at
    most 6,000 -- and a MERGE whose source holds a key twice is a Snowflake
    error, not a silent dedup.

    Deduplicated in Spark rather than with `DISTINCT ON` in the subquery: the
    JDBC partition predicate is applied OUTSIDE the subquery, so each of the
    eight partitions would run the distinct over the whole table.
    """
    ranked = raw.withColumn(
        "rn",
        F.row_number().over(Window.partitionBy("fixture_id").orderBy(F.col("ingested_at").desc())),
    )
    return ranked.filter(F.col("rn") == 1).drop("rn")


def parse(raw: DataFrame) -> DataFrame:
    """One row per finished fixture, with the response struct unpacked."""
    return (
        latest_per_fixture(raw)
        .select(F.from_json("payload", _PAYLOAD).alias("p"))
        .select(F.col("p.response")[0].alias("r"))
        .filter(F.col("r.fixture.status.short").isin(*FINISHED))
        .filter(F.col("r.teams.home.id").isNotNull() & F.col("r.teams.away.id").isNotNull())
    )


def team_statistics(fixtures: DataFrame) -> DataFrame:
    """`(fixture_id, team_id)` with one column per statistic label.

    A pivot rather than a UDF: the labels are known, and keeping it in Spark
    expressions means the 505 MB never crosses into Python.
    """
    exploded = (
        fixtures.select(
            F.col("r.fixture.id").alias("fixture_id"),
            F.explode("r.statistics").alias("block"),
        )
        .select(
            "fixture_id",
            F.col("block.team.id").alias("team_id"),
            F.explode("block.statistics").alias("stat"),
        )
        .select(
            "fixture_id",
            "team_id",
            F.col("stat.type").alias("label"),
            # Values arrive as "52%", "1.4", or null. Strip the percent sign
            # and let the cast yield null for anything unparseable, rather than
            # failing the whole job for one odd row.
            F.regexp_replace(F.col("stat.value").cast("string"), "%", "")
            .cast("double")
            .alias("value"),
        )
        .filter(F.col("label").isin(*STAT_COLUMNS.keys()))
    )
    pivoted = (
        exploded.groupBy("fixture_id", "team_id")
        .pivot("label", list(STAT_COLUMNS.keys()))
        .agg(F.first("value"))
    )
    for label, column in STAT_COLUMNS.items():
        pivoted = pivoted.withColumnRenamed(label, column)
    return pivoted


def _side(fixtures: DataFrame, is_home: bool) -> DataFrame:
    """One team's view of every fixture."""
    us, them = ("home", "away") if is_home else ("away", "home")

    def score(container: str, side: str) -> Column:
        return F.col(f"r.score.{container}.{side}")

    reg_for, reg_against = score("fulltime", us), score("fulltime", them)
    ht_for, ht_against = score("halftime", us), score("halftime", them)
    et_for, et_against = score("extratime", us), score("extratime", them)

    return fixtures.select(
        F.col("r.fixture.id").alias("fixture_id"),
        F.col(f"r.teams.{us}.id").alias("team_id"),
        F.col(f"r.teams.{us}.name").alias("team_name"),
        F.col(f"r.teams.{them}.id").alias("opponent_id"),
        F.col(f"r.teams.{them}.name").alias("opponent_name"),
        F.lit(is_home).alias("is_home"),
        F.to_timestamp("r.fixture.date").alias("match_date"),
        F.col("r.league.season").alias("season"),
        F.col("r.league.name").alias("league_name"),
        F.col("r.league.round").alias("round"),
        F.col("r.fixture.status.short").alias("status"),
        ht_for.alias("score_halftime_for"),
        ht_against.alias("score_halftime_against"),
        reg_for.alias("score_fulltime_for"),
        reg_against.alias("score_fulltime_against"),
        et_for.alias("score_extratime_for"),
        et_against.alias("score_extratime_against"),
        score("penalty", us).alias("score_penalty_for"),
        score("penalty", them).alias("score_penalty_against"),
        # D18 components. `reg` is the 90-minute score and the DEFAULT
        # settlement basis; `full` adds extra time and is selected only by
        # markets that declare it. Penalties enter neither (D30).
        ht_for.alias("goals_for_h1"),
        (reg_for - ht_for).alias("goals_for_h2"),
        reg_for.alias("goals_for_reg"),
        et_for.alias("goals_for_et"),
        score("penalty", us).alias("goals_for_pens"),
        (reg_for + F.coalesce(et_for, F.lit(0))).alias("goals_for_full"),
        ht_against.alias("goals_against_h1"),
        (reg_against - ht_against).alias("goals_against_h2"),
        reg_against.alias("goals_against_reg"),
        et_against.alias("goals_against_et"),
        score("penalty", them).alias("goals_against_pens"),
        (reg_against + F.coalesce(et_against, F.lit(0))).alias("goals_against_full"),
        F.coalesce(reg_for, F.col(f"r.goals.{us}")).alias("goals_for"),
        F.coalesce(reg_against, F.col(f"r.goals.{them}")).alias("goals_against"),
    )


def team_rows(fixtures: DataFrame) -> DataFrame:
    """Two rows per fixture, joined to their statistics and the opponent's xG."""
    sides = _side(fixtures, True).unionByName(_side(fixtures, False))
    with_stats = sides.join(team_statistics(fixtures), ["fixture_id", "team_id"], "left")

    # xg_against is the OPPONENT's xG, which only exists once both sides carry
    # their statistics -- hence a self-join rather than another column.
    opponent_xg = with_stats.select(
        F.col("fixture_id"),
        F.col("team_id").alias("opponent_id"),
        F.col("xg").alias("xg_against"),
    )

    return (
        with_stats.join(opponent_xg, ["fixture_id", "opponent_id"], "left")
        .withColumn(
            "result",
            F.when(F.col("goals_for") > F.col("goals_against"), F.lit("W"))
            .when(F.col("goals_for") < F.col("goals_against"), F.lit("L"))
            .otherwise(F.lit("D")),
        )
        .withColumn(
            # The per-team keeper signal. `goals_prevented` itself is
            # fixture-level (D16) and cannot be attributed to one team.
            "goals_against_minus_xg_against",
            F.col("goals_against") - F.col("xg_against"),
        )
    )


def main() -> None:
    spark = session()
    try:
        # Cached because three branches read it -- both sides and the
        # statistics pivot -- and without this the 505 MB JSON parse runs
        # once per branch.
        fixtures = parse(read_payloads(spark)).cache()
        rows = team_rows(fixtures)
        # toPandas is typed as returning a protocol rather than a concrete
        # DataFrame, so the narrowing is stated once here instead of at every
        # use below.
        frame: pd.DataFrame = rows.toPandas()
    finally:
        spark.stop()

    log.info("flattened %d team-match rows", len(frame))

    usable = frame.dropna(subset=list(REQUIRED))
    dropped = len(frame) - len(usable)
    if dropped:
        log.warning("dropped %d rows missing a required column", dropped)

    records = usable.astype(object).where(usable.notna(), None).to_dict("records")
    with connect() as warehouse:
        written = merge_bulk(
            warehouse,
            table="fact_team_match",
            rows=records,
            key=["fixture_id", "team_id"],
        )
    log.info("fact_team_match=%d", written)


if __name__ == "__main__":
    main()
