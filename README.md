# arbibet-capstone

**Sports Information Platform — a minimum-viable data engineering reference implementation.**

Reads the append-only bronze layer of an existing multi-bookmaker collection
platform, normalises five bookmakers' incompatible market schemes into a single
canonical market/outcome taxonomy, and detects cross-book arbitrage and
positive EV **within seconds of a new price**, woken by Postgres NOTIFY. One
always-on runner keeps a local **DuckDB** warehouse current (PySpark for match
history, dbt for the gold layer, OpenAI for grounded summaries) and publishes
what the dashboard shows to **Supabase**, which a Streamlit dashboard reads --
including a Pipeline health page in place of the Airflow UI.

The platform ran on Snowflake, Kafka and Airflow first. It moved to DuckDB and
a runner in September 2026, when the cloud warehouse's metered compute proved
the wrong fit for 221 MB of data; see [Why DuckDB and Supabase](#why-duckdb-and-supabase).

Built as the capstone for the Ironhack Data Engineering bootcamp (Jul-Sep 2026),
in a 2.5-day focused sprint. Full plan: [`docs/capstone-plan.md`](docs/capstone-plan.md).

> **Data framing.** Sports data and market signal, not betting infrastructure.
> Bookmaker prices are used as the highest-frequency signal source available in
> the domain; the analytical outputs are about market efficiency, per-source
> drift, and insight generation. This is a reference implementation, not a
> production consumer service.

---

## Architecture

```mermaid
flowchart TB
  subgraph SRC["Sources -- read-only"]
    bronze[("arbibet-markets bronze<br/>5 books · verbatim BYTEA")]
    match[("arbibet-matcher<br/>event_matches")]
    ingestor[("api-football-ingestor<br/>nested match payloads")]
    msport{{"msport booking codes"}}
  end

  subgraph RUN["Runner -- one always-on process in Docker"]
    listener["listener<br/>Postgres LISTEN"]
    hot["hot loop<br/>surebets and EV in seconds"]
    warm["warm loop, 15 min<br/>dims · slips · ticks · dbt · AI · publish"]
    cold["cold loop, daily<br/>Spark flatten + settle · backup"]
  end

  subgraph WH["DuckDB warehouse -- local file"]
    core[("core<br/>dims · signals · ticks · history")]
    lake[("Parquet lake<br/>slip payloads")]
    gold[("analytics<br/>dbt staging + gold")]
    ops[("ops<br/>job_run · heartbeat")]
  end

  ai["OpenAI gpt-4o-mini<br/>grounded summaries"]
  sb[("Supabase Postgres<br/>serving documents · deep dives<br/>flags · ops mirror")]
  st["Streamlit dashboard<br/>signals · slips · pipeline health"]
  sf[("Snowflake<br/>frozen capstone artifact")]

  bronze -- "NOTIFY per payload" --> listener --> hot
  bronze --> hot
  match --> warm
  msport --> warm
  ingestor --> cold
  hot --> core
  warm --> core
  warm --> lake
  cold --> core
  core --> gold
  lake --> gold
  gold --> ai --> core
  hot -. heartbeats .-> ops
  warm -. runs .-> ops
  gold --> sb
  ops --> sb
  sb --> st
  st -- "viewer flags" --> sb

  classDef store fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
  classDef proc fill:#dcfce7,stroke:#16a34a,color:#14532d
  classDef cloud fill:#f3e8ff,stroke:#9333ea,color:#581c87
  classDef old fill:#f1f5f9,stroke:#94a3b8,color:#475569,stroke-dasharray: 4 3
  class bronze,match,ingestor,core,lake,gold,ops store
  class listener,hot,warm,cold,ai proc
  class sb,st cloud
  class sf old
```

**Reading key:** blue = data stores; green = processes; purple = the free cloud
tier the public dashboard runs on; grey dashed = kept, no longer written.

### The problem this platform actually solves

Three of the five bookmakers publish market and outcome identifiers from the
betradar taxonomy natively. Two -- bet9ja and livescorebet -- publish
proprietary schemes: bet9ja keys markets as strings like `S_OU@2.5`,
livescorebet as integer `type` codes. Until those are reconciled no
cross-bookmaker comparison is possible at all, because "Over 2.5 goals" is a
different string at every book.

A 236-row crosswalk maps both dialects onto the betradar space. Everything
downstream -- arbitrage detection, positive-EV screening, the gold aggregates --
is only meaningful because that reconciliation happened first.

---

## Repository layout

| Path | What it is |
|---|---|
| `src/arbibet_capstone/bronze.py` | Reads the latest payload per bookmaker for one fixture |
| `src/arbibet_capstone/crosswalk/` | **Vendored** parsers, arbitrage engine, and the crosswalk CSV |
| `runner/` | **The pipeline**: listener, hot/warm/cold loops, Supabase publisher, observability, Docker image |
| `sql/` | DuckDB schema, the bronze NOTIFY trigger, the Supabase dashboard role |
| `src/arbibet_capstone/warehouse.py` | DuckDB connection, idempotent MERGE writers, the Parquet slip lake |
| `producer/`, `consumers/` | The original Kafka path (Snowflake era); the hot loop replaced it |
| `slips/ingest.py` | Fetches msport booking codes, stored verbatim |
| `spark/flatten.py` | Flattens the ingestor's nested match payloads into `fact_team_match` |
| `spark/settle.py` | Settles historical markets per team via the vendored silver resolvers |
| `enrich/summarise.py` | LLM verdict per slip, grounded in `gold_slip_leg_history` |
| `dbt/` | Staging, `gold_market_efficiency`, `gold_slip_leg_history`, 28 tests |
| `airflow/dags/` | The Snowflake-era DAGs, kept for reference; no longer run |
| `dashboard/` | Streamlit app, reading Supabase |
| `publish/snapshot.py` | Builds the serving documents: signals, slips, deep dives |
| `web/` | A Next.js front end over the same documents, not deployed |
| `snowflake/` | The Snowflake schema and role, kept as the record of that deployment |
| `scripts/copy_snowflake_to_duckdb.py` | The migration, verified table by table |
| `scripts/go_no_go.py` | The gate that proves cross-book resolution works |

---

## Quickstart

```bash
cp .env.example .env          # DSNs, OPENAI_KEY, SUPABASE_DB_URL (transaction pooler)
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev,spark]" duckdb dbt-duckdb
pytest                        # 117 tests; the warehouse ones use a temporary DuckDB
```

Once per machine:

```bash
python scripts/install_bronze_notify.py   # NOTIFY trigger on markets bronze
# Supabase SQL editor: run sql/supabase_dashboard_role.sql with your own password
```

Then the whole pipeline is one container, which restarts with Docker:

```bash
docker compose up -d --build runner
docker compose logs -f runner
docker exec arbibet-runner python -m runner.status
```

The warehouse lives in the `arbibet-warehouse` Docker volume rather than a bind
mount: DuckDB's file locking and random I/O are unreliable across the Windows
file share. One-off cycles for development, against `data/arbibet.duckdb`:

```bash
python -m runner.main --warm-once
python -m runner.main --cold-once
```

Only one process may open the DuckDB file for writing, so every job -- dbt and
Spark included -- runs inside the runner rather than beside it.

## Operations and observability

The Airflow UI is replaced by records the runner keeps itself, published to
Supabase and shown on the dashboard's **Pipeline health** page:

- **`ops.heartbeat`** -- one row per component (runner, listener, hot, warm,
  cold), overwritten on every beat. A component whose beat is older than its
  schedule shows as stale.
- **`ops.job_run`** -- every job execution: start, end, status, rows, error. The
  hot loop writes one summary row a minute with its wakes, fixtures recomputed,
  rows written, and **latency from bronze storing a payload to the signal being
  stored** -- a median of 2.5-5 s on the first live runs.
- **Logs** -- `docker compose logs runner`, and rotated files under the volume's
  `logs/`.
- **`python -m runner.status`** -- the same picture in a terminal, read from a
  status file, because a second process cannot open the DuckDB file.

A failed job is recorded and the cycle moves on; the next cycle retries it. A
dead hot loop is restarted by the supervisor thread. A dropped NOTIFY
connection reconnects with backoff while the 15-second poll keeps signals
flowing.

## Why DuckDB and Supabase

| | Snowflake (before) | DuckDB (warehouse now) | Supabase (serving now) |
|---|---|---|---|
| Cost | $212 of trial credit in 16 days | $0 | $0, free tier |
| Size used | 221 MB | 79 MB + 4 MB Parquet | about 17 MB of 500 MB |
| Role | everything | transformations, history, signals | only what the dashboard reads |
| Reachable from the cloud dashboard | yes, metered per visit | no, a local file | yes |

The data never needed a cloud warehouse; the bill was compute kept awake by
frequent small jobs and a polling dashboard. DuckDB does the same SQL locally
and for free, with `QUALIFY` and JSON support close enough to Snowflake that
the dbt port was mostly function renames, verified by identical gold row
counts. Supabase holds only the published documents, so a public dashboard
costs nothing per visit. Snowflake keeps its data as the record of the
original deployment; `snowflake/` and the `snowflake-final` git tag are the
code that ran against it.

## The dashboard

Three pages -- Market signals, Betting slips, Pipeline health -- reading the
documents the runner publishes to Supabase. The screenshots below were captured
in the Snowflake era; the pages are the same. Regenerate the images with
`python docs/capture_screenshots.py` while the app is running -- they are
scripted rather than hand-captured because every number on the page moves, so a
hand-framed screenshot silently ages while the README goes on citing it.

![Overview](docs/screenshots/01-overview.png)

Headline counts, then cross-book overround by market. The one negative bar is
first-half corners: the obscure market where soft books pay least attention,
and where both of the best surebets were found. Only TRUE surebets are counted
-- the consumer records near misses down to 0.98 so market efficiency can be
measured, but a headline counting them would read as 25 opportunities when
there were four.

![The signals themselves](docs/screenshots/02-signals.png)

One card per signal rather than one row per leg: arbitrage, spread and
detection time are properties of the SET, and repeating them on every leg
invited reading four legs as four opportunities. **Spread** is the gap between
the oldest and newest price in the set -- how far from simultaneous the
snapshot was. Nothing is filtered on it, wide ones included, because hiding
them would flatter the result.

Outcome names come from the bookmakers themselves. The settlement taxonomy
names outcomes only for markets the engine can settle, and it settles from
scores, so corners arrive with ids and no names -- this table used to read
`outcome 12`. Resolving the id alone would be a confident lie: id 12 means
`over` in one market and `1-2` in another.

![Line movement](docs/screenshots/03-line-movement.png)

How the price got to where the signal caught it: every price change all five
books published for one outcome, replayed from bronze through the same
crosswalk that produced the signal. Step interpolation, because a price holds
until the book moves it; Plotly, because the series run to hundreds of points
over days and the interesting parts are minutes wide. The dashed line is
kick-off.

![Deep dives](docs/screenshots/04-deep-dives.png)

The five fixtures the most booking slips were built on, each with its own page.
Ranked by how many DISTINCT slips name the fixture -- one hugely-copied slip
should not make its fixtures look widely backed.

![Slip verdicts](docs/screenshots/05-slip-verdicts.png)

The LLM panel, with the model's input beside its output -- deliberately, so a
reader can judge the verdict rather than take it. That pairing is a correctness
feature: it is what caught a stale summary describing evidence that had since
gone to NULL.

### A fixture deep dive

Five fixtures, one page each, reached by link.

![Fixture brief](docs/screenshots/06-fixture-brief.png)

A prose brief written by `gpt-4o-mini` from **this warehouse and nothing else**:
the price series in `fact_odds_tick`, the match history in `fact_team_match`,
the settled market rates in `fact_team_market_result`. A football fixture is
exactly the sort of subject a language model already has opinions about, so the
system prompt forbids outside knowledge in those terms -- no league position,
no injuries, no past meetings -- and the user prompt carries every number the
answer is allowed to contain. It is asked to keep prices and results apart
rather than average them into a verdict, because the gap between them is the
only interesting thing on the page.

![Fixture market](docs/screenshots/07-fixture-market.png)

Every book's price for every outcome of one market, faceted so the whole market
is legible at once rather than overlaid on a single axis. The dashed line is
kick-off, in Europe/Berlin -- which is not a detail: Snowflake's session
timezone defaults to `America/Los_Angeles`, and until it was pinned this
evening fixture displayed an 11:45 kick-off. No instant was ever stored wrong;
every wall clock printed nine hours early. See FINDINGS 6b.

![Fixture history](docs/screenshots/08-fixture-history.png)

And what both sides had actually been doing beforehand, from API-Football, plus
how the same betradar markets have really settled for them. Only matches BEFORE
kick-off count: using a side's later results to judge how a fixture was priced
is the most inviting mistake on the page.

## What the pipeline found

**Arbitrage is approximately zero, and that is the answer.** The first run
flagged 174 opportunities, the worst at 7.83x -- betting every outcome for 783%
of stake. Every single one was a crosswalk defect: an over/under ladder
collapsed into one market, an asian handicap not normalised to the home frame,
a suspended market read as a live price. After three parser fixes: **25 signals,
4 of them true surebets, the best at 1.0396** -- and both of the best sit on
first-half corners, the obscure market where soft books pay least attention.

**Popularity and soundness are uncorrelated.** 5,739 people copied a slip with
a 1-in-411,956 chance of winning. The most-followed slip on the same fetch was
a sane 1-in-13. That is what makes "does this slip make sense?" a real question
rather than a gimmick.

**Legs are not simultaneous.** Bronze writes only when a payload changes, so a
book whose price has not moved contributes a stale leg. Observed spreads reach
108,927 seconds -- 30 hours. Every arbitrage row carries the spread, and the
gold model reports overround twice, whole and fresh-only, so the cost of stale
legs is visible rather than filtered away.

## Honesty statement

**What a row in `fact_arbitrage_signal` means.** `ARB_RECORD_THRESHOLD` is
0.98, so the table records **near-arbitrage** -- the shortfall left after
shopping every book -- not only guaranteed returns. `arbitrage > 1` marks a
true surebet; everything below is cross-book disagreement, which is the thing
worth measuring given surebets barely exist. Reading the row count as an
opportunity count would overstate the platform by roughly six to one.

**What was reused, not built.** The bronze collection layer, the cross-book
fixture matcher, the five bookmaker parsers, the 236-row market crosswalk, the
arbitrage/EV engine, and arbibet-silver's 4,874-line settlement engine all
predate this capstone and belong to the wider Arbibet platform. This repository
is the pipeline *around* them.

**What is new here.** The bronze-to-Kafka producer and consumers (Snowflake
era), the runner and its NOTIFY-driven hot loop, both warehouse schemas, the
Spark flatten and settle jobs, the slip ingest, every dbt model, the LLM
summarisers, the serving publisher, CI, the DAGs and the dashboard.

**Authorship.** Architecture, specification and verification are the author's;
much of the implementation was produced through a spec-driven
coordinator-implementor-verifier workflow with AI implementors.

**What the numbers do not say.** `historical_rate` is a base rate over at most
ten matches and knows nothing about opponent strength -- Osnabruck have won 5
of 10 and are 38.89 to beat Bayern Munich. It is recent form, never a
probability for the fixture in hand. Slip probabilities multiply msport's own
per-leg numbers, which embeds their margin and assumes the legs are
independent; two legs on the same match are not. And `bettableBetSlip` is the
**remnant** of a slip, not the original -- legs vanish at kickoff -- so the
metric is copyability now, not whether the slip was ever sound.

**Coverage.** Only 24,672 of 297,663 team-match rows carry xG, because
API-Football populates statistics for some leagues and not others. 91% of slip
legs resolve to a fixture; 88% reach a settled history.

**Scope deferred.** AWS, IaC, Iceberg, NiFi, Spark Structured Streaming and
Great Expectations are absent by decision. See the roadmap.

## Cost report

| Component | Cost |
|---|---|
| Snowflake | $212 of the $400 trial credit over 16 days, then retired -- see above |
| DuckDB, Spark, dbt, runner, Postgres | $0, local |
| Supabase | $0, free tier |
| Streamlit Community Cloud | $0 |
| OpenAI `gpt-4o-mini` | **about $1** to mid-September 2026 |

Snowflake Cortex was the original plan for the AI layer and would have been
$0. Cortex AI functions are unavailable on trial accounts -- verified in the
console, both `COMPLETE` and `SENTIMENT` refuse on account type. The summariser
moved to a task calling OpenAI, which put the LLM behind a swappable boundary:
replacing it with a locally-hosted model is a one-file change.

## Post-capstone roadmap

Deferred by design, in the order they would be picked up. Each closes a distinct
gap.

1. **NiFi normalisation flow** -- move parse and crosswalk out of the producer
   into a NiFi stage between a raw and a canonical topic.
2. **AWS Lambda + S3 + Terraform** -- the api-football fetch as a scheduled
   container Lambda landing raw JSON in S3, all provisioned as IaC.
3. **Iceberg bronze on S3** -- register that S3 output as an Iceberg table via
   the Glue Catalog.
4. **Great Expectations** -- silver and gold suites wired into the DAG.
5. **Spark Structured Streaming freshness monitor** -- a third consumer doing
   stateful windowed per-book freshness.
6. **Extend the crosswalk past five books** -- bronze already collects fourteen.
   Each addition is one parser plus its crosswalk rows, and widens the price
   spread that arbitrage depends on.
7. **Swap OpenAI for a locally-hosted model** -- the summariser is already a
   task, so this is a one-file change. That is the argument for having put the
   LLM behind a task rather than inside a SQL function.

---

## Two rules this codebase enforces

**Never filter bronze by `bookmaker` alone.** The indexes on
`bronze_event_payloads` are `(event_id, bookmaker, write_time DESC)`,
`(fire_time)` and `(event_id, bookmaker, payload_sha256)` -- none leads with
`bookmaker`, so a book-only filter degrades to a sequential scan over the BYTEA
payload bodies. That query took the production host down once.

**Bronze's bookmaker spelling is canonical.** `msport`, not `msports`;
`ilotbet`, not `ilobet`. The matcher uses the other spellings and
arbibet-markets translates them once on the way in, so everything downstream of
bronze -- this repository included -- uses bronze's vocabulary and never
introduces a second one.

---

## Development

```bash
pip install -e ".[dev]"
pytest          # no database required
ruff check .
mypy
```

`src/arbibet_capstone/crosswalk/` is vendored from the Arbibet platform and is
excluded from `ruff` and `mypy --strict`. Typing it would mean rewriting it, and
not rewriting it is the point of vendoring. Everything this repository authors
stays under strict.

Upstream databases are read-only: `bronze.connect()` sets
`default_transaction_read_only` on the session, so a stray write is refused by
Postgres rather than caught in review.

### Match history and market resolution

The LLM summaries are grounded in two things, both derived from
`api-football-ingestor`'s `bronze_fixture_details` (154,088 nested payloads,
829 leagues, current to today):

**Statistical form** -- `fact_team_match`, one row per (fixture, team). Carries
the four score containers API-Football publishes, the period components those
resolve into, and the full statistics block: xG, possession, shots by type,
passes, cards, corners, saves. How a side has been playing, not only what it
scored.

**Market resolution** -- `fact_team_market_result`, one row per team per
settled market. The same betradar market families the bookmakers are pricing
right now, settled against historical results. That is what lets a summary say
"these sides have gone over 2.5 in seven of their last ten" rather than "they
have won three of five".

The settlement engine is arbibet-silver's, vendored whole: `settlement.py`,
`team_perspective.py` and the two betradar maps. They are pure functions with
no I/O -- silver enforces that with an AST test -- so they port without
modification. Two of their decisions are load-bearing here:

- **Period-resolved scores.** A market declares what it settles on; 1st-half
  markets settle on `h1`, and knockouts settle differently on regular versus
  full time. Silver measured an AET final where 3 of 6 markets settled
  differently on the two bases, which is why one collapsed score is not enough.
- **Refusal over default.** A market family the engine has not classified is
  refused with a reason, never silently treated as symmetric. Unsettleable rows
  are kept -- they are the map's to-do list, not noise.

Two column names look odd and are deliberate, per silver's D16:
`goals_prevented_fixture` is fixture-level because API-Football reports it
identically for both teams, and `passes_accurate` is a count, not a percentage.

Not extracted yet: `events`, `lineups` and `players`. Those are the first
post-capstone job.

### Spark

`spark/dim_refresh.py` runs PySpark in **local mode** -- there is no cluster.
For a 236-row dimension load a standalone master and worker would be ceremony,
and local mode still exposes the Spark UI on `:4040` while a job runs.

Three environment facts it depends on, all set in `.env`:

| Requirement | Why |
|---|---|
| **PySpark 4.x**, not 3.5 | PySpark 3.5's classifiers stop at Python 3.11; on 3.12 the worker crashes with `Python worker exited unexpectedly` |
| **JDK 17** on `JAVA_HOME` | Spark needs Java 17 or 21. Java 18+ breaks `Utils.getCurrentUserName`, and Java 24 removed the Security Manager the common workaround relied on |
| **`PYSPARK_PYTHON`** = the venv interpreter | Otherwise Spark launches bare `python` from PATH and the worker dies with a socket reset |

The `winutils.exe` / `HADOOP_HOME` warning on Windows is benign for JDBC work
and can be ignored.
