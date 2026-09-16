# Findings from the bronze / producer / consumer session

Context another session cannot recover by reading the code.

**Read this before touching the crosswalk, the arbitrage engine, anything
Spark-related, or the settlement resolvers.** Sections 14 and 15 are the
handoff: what is built, what is left, and how to run any of it. Sections 1-13
are the durable findings -- each one is something that produced a *plausible
wrong number* rather than an error, which is why none of them is discoverable
from a passing test suite.

Written 2026-09-02; sections 10-15 added 2026-09-03.

---

## 1. Four defects were found and fixed. Do not undo them.

Each was invisible until `parse_bookmaker` stopped swallowing exceptions, and each
produced a *plausible wrong number* rather than an error. If a later change
"simplifies" any of these, the phantom arbitrage comes back.

**a. `crosswalk/parsers/livescorebet.py` — the line is part of the market's identity.**
Grouping was on `marketId` alone, so Over 0.5 through Over 6.5 all became outcome `12`
of market `18;0.5`, tagged with whichever line came last. Best-price comparison then
paired Over 6.5 at 21.0 against Under 0.5 at 11.0 and reported a **7.8x arbitrage**.
The groupby key is now `["marketId", "hcp"]` with `dropna=False` — the `dropna`
matters, or markets with no line (1X2, BTTS) are silently dropped.

**b. `crosswalk/parsers/livescorebet.py` — asian handicap must be normalised to the
home frame.** livescorebet states each side's line from *that side's* perspective, so
its away row belongs to the opposite line; every other book states both sides in the
home frame. Pairing "Burnley -0.5" with "Middlesbrough -0.5" read as a **30%
arbitrage** on a mainstream market at five books. `_canonical_line` negates the away
side for market 16 and formats with `:g` so both sides land on one market id. The
legacy `docs/legacy/markets.py` did this; the refactor into `parsers/` lost it.

**c. `crosswalk/parsers/sportybet.py` — suspended markets are not prices.** sportybet
publishes the same `(id, specifier)` once per betting *product*, and only one is live;
stale entries keep their last prices. Observed `18/total=3.5` at 1.51 (`status 0`)
beside 3.50 (`status 1`, last changed 8.4 hours earlier) — a 30% phantom arbitrage
against books quoting the live line. The outcome-level `isActive` check does NOT catch
this: the outcomes of a suspended market are themselves flagged active. Now skips
`status not in (0, None)`.

**d. `crosswalk/arbitrage.py` — the pre-filter summed over every row.** It computed
`1/sum(1/odds)` across every book's every outcome, so with N books the result was about
1/N of the truth and **more books made detection less likely**. It flagged zero markets
on live data, ever. It now takes the best price per outcome, which is the question
`assign_unique_bookmakers_for_market` already answers downstream.

**msport probably has defect (c) too** — its parser also filters only on `isActive`.
Not fixed, because it deserves the same evidence-gathering sportybet got.

---

## 1b. A fifth defect: one book publishes 0.00 for a suspended outcome.

Found by an axis. The line-movement chart's y-scale is declared `zero=False`,
and it was reaching zero anyway -- which meant the data contained a zero.

**38 of the first 7,765 ticks had `odds = 0.0`, all from the same book, plus 4
at exactly `1.0`.** Neither is a price. Decimal odds of 1.00 return the stake
and nothing else; 0.00 returns nothing at all. They are how that book says
"this outcome is not currently available" -- the same *class* of defect as
sportybet's stale suspended ladder in section 1, expressed differently.

**Charted**, they draw a price collapsing to zero and recovering: a suspension
rendered as a violent market move, at exactly the moment a reader is most
interested.

**In the engine, it is worse and still unfixed.** `crosswalk/arbitrage.py`
drops NaN odds (`dropna(subset=["id", "odds"])`) but not zero ones. A 0.00 leg
gives `1/0 = inf`, the sum of inverses is `inf`, and `arbitrage = 1/inf = 0` --
far below any threshold. So the market is **silently never flagged**. This
produces FALSE NEGATIVES, not false positives, which is why nothing caught it:
the failure mode is a signal that never appears.

Nobody has measured how many opportunities this has suppressed.

**Fixed here:** `ticks.price_changes` drops `odds <= 1.0`, the dbt test
`assert_odds_are_real_prices.sql` fails if any reach the table, and
`tests/test_ticks.py` pins the behaviour. **Not fixed upstream:** the same
values still reach the arbitrage and EV consumers. The proper repair is in the
markets repo's parser, alongside the four in section 1, and it is deliberately
not done here -- the parsers are vendored, and diverging the copy mid-capstone
would be worse than a recorded known gap.

---

## 1c. The odds tick store: `odds/ticks.py`.

**Bronze was already a tick store; nobody had noticed.** `bronze_event_payloads`
is append-only and writes a row whenever a book's response changes, so the
price history is there for free. The 11 fixtures behind our arbitrage signals
carry **4,984 payloads**, 150-230 per book, over about ten days. No new
collection was built.

`odds/ticks.py` replays that history through the **same crosswalk the producer
uses**, which is the point: a chart and a signal parsed by one code path can
only disagree for real reasons, never because two implementations drifted.

Three decisions worth keeping:

* **A tick is a price CHANGE, not a payload.** Bronze writes when any part of
  a response moves, so consecutive payloads routinely repeat the price of the
  outcome being charted. Collapsing them took 4,984 payloads to 7,723 ticks
  instead of several times that, and it is what makes the step chart legible.
* **Scope is bounded to markets that appear in a signal** (`UNION` of both
  signal tables). Every market in every payload would be a few hundred
  thousand rows nobody reads. Runtime is ~2 minutes for all 11 fixtures.
* **Parse failures are counted and returned, not raised** -- the opposite of
  `snapshot.build`, and deliberately. `snapshot.build` reads the CURRENT
  payload, where a failure means the pipeline is broken now and must stop.
  This replays weeks of history, where a body written under an older response
  shape is ordinary. `Extraction.failed_payloads` stops that being an excuse:
  the first full run reported **0**.

**It is INCREMENTAL, and the cursor alone would have been wrong.** The first
version replayed every payload for every in-scope fixture on every run: 9,732
payloads and 48 minutes to derive ~4,800 new rows, about 95% of the work
re-deriving rows already in the table. Runtime grew 27 -> 48 minutes across two
runs while the payload count grew only 7%, so most of that was contention, not
volume -- but the structure was the problem either way.

Each fixture now resumes from `max(fire_time)` already stored for it
(`_CURSORS`), which takes a daily run from ~9,700 payloads to 7-11 per fixture.

**The trap:** a cursor by itself makes the run WRONG, not merely partial.
`price_changes` emits a tick when a price differs from the previous one it
saw, so a run starting mid-history with no memory treats the first payload
after the cursor as a change and writes a phantom tick for a price that never
moved. The state is therefore seeded from the last stored price per
`(fixture, book, market, outcome)` (`_SEEDS`), after which the series continues
exactly as a full replay would -- and a small overlap at the cursor costs
nothing, which is what makes the `fire_time` filter safe despite late arrivals.
Both halves are pinned by tests: a seeded resume emits nothing for an unchanged
price, and still reports a real move.

`TICKS_FULL_REPLAY=1` rebuilds from the start of bronze, which is what a parser
fix requires -- every stored tick was derived by the old code.

**It must run after the consumers**, since the consumers decide which markets
are in scope. The DAG wires `[consume_arb, consume_ev] >> extract_ticks >>
dbt_run`.

**Bronze prunes.** History reaches back roughly seven weeks; a fixture older
than that returns no rows rather than an error, and `odds/ticks.py` logs a
warning so it does not look like a fixture whose price never moved.

---

## 2. Arbitrage is approximately zero. Design around that, not against it.

Measured across 31 fixtures with 2+ books, after each fix:

| state | markets flagged | worst value |
|---|---:|---:|
| before any fix | 174 | 7.83 |
| after (a) | 54 | 1.32 |
| after (b) | 1 | 1.0000 |

That last one is exactly 1.0000 — breakeven. **Every one of the original 174 was a
crosswalk defect.** Genuine surebets across public books hours before kickoff do not
meaningfully exist.

Consequence: `ARB_RECORD_THRESHOLD` defaults to **0.98**, so the table records *near*
arbitrage — the best-price shortfall, a real measure of cross-book disagreement.
`arbitrage > 1` distinguishes a true surebet. **This must be stated in the README**; it
changes what a row means.

Corollary: the **EV consumer and the booking-slip analysis are where the signal is**,
not arbitrage.

---

## 3. Leg freshness is not cosmetic.

Bronze writes a row only when a payload *changes*, so two books' "latest" payloads can
be minutes or hours apart. Observed spread on real signals: **0 to 108,927 seconds**
(30 hours). An arbitrage across a fresh leg and a 30-hour-old leg is an artefact.

That is why `fire_time` travels per book from `bronze.latest_payloads` all the way to
`fact_arbitrage_signal.leg_spread_seconds`. Any gold model or dashboard over these
signals needs a freshness filter, and the presentation should name the limitation
rather than wait to be asked about it.

---

## 4. Bookmaker naming: bronze is canonical.

| layer | MSport | iLotBet |
|---|---|---|
| Go collector, `event_matches` | `msports` | `ilobet` |
| **arbibet-markets bronze** | **`msport`** | **`ilotbet`** |

arbibet-markets translates once, in `watch_set.py::_MATCHER_BOOKMAKER_ALIASES`. Bronze
sits downstream of that, so its spelling is already canonical and this project uses it
everywhere — parser registry, `fill_probabilities`, `dim_bookmaker`. A second
translation layer was written and then deleted. Do not reintroduce one.

Do **not** "fix" the Go collector to emit `ilotbet`. It changes `e_id`, the matcher's
identity key (D4), which feeds `match_key` hashing — the same fixture would acquire a
second identity. It would also only affect new rows.

---

## 5. Access paths that will bite.

**`bronze_event_payloads`** — indexes are `(event_id, bookmaker, write_time DESC)`,
`(fire_time)`, `(event_id, bookmaker, payload_sha256)`. **Nothing leads with
`bookmaker`.** A book-only filter sequentially scans 500 MB of BYTEA. That query took
the production host down once.

**`bronze_fixture_details`** (ingestor, port 5434) — the only index is
`(fixture_id, ingested_at DESC)`. A date-only filter is a full scan of 505 MB. Fine for
a batch flatten; never in a query path.

**`event_matches`** — `(sport_key, start)` is the index. The GIN index on
`bookmaker_event_ids` is for containment queries, which the fixture list deliberately
does not do.

---

## 6. Environment traps, all hit and resolved.

- **PySpark must be 4.x.** 3.5's classifiers stop at Python 3.11; on 3.12 the worker
  dies with `Python worker exited unexpectedly` and no Python traceback.
- **JDK 17, on a path with no spaces.** Java 18+ breaks Spark at
  `Utils.getCurrentUserName`, and Java 24 removed the Security Manager that the usual
  `-Djava.security.manager=allow` workaround relied on.
- **`PYSPARK_PYTHON` must point at the venv interpreter**, or Spark launches bare
  `python` from PATH and the worker dies with a socket reset.
- **In bash, quote `JAVA_HOME`** — unquoted `C:\jdk...` loses the backslash to shell
  escaping and yields "the system cannot find the path specified".
- **`.env` is not read automatically.** Entry points call `arbibet_capstone.env.load()`;
  library modules never do.
- **`MSYS_NO_PATHCONV=1`** is needed for `docker run` with absolute container paths in
  Git Bash, or `/bin/bash` is rewritten into a Windows path.
- **Never edit files with Python `read_text()` / `write_text()` without an encoding** —
  Windows defaults to cp1252 and corrupts any non-ASCII character.
- **`spark.jars.packages` is fatal on Windows.** It makes SparkSubmit resolve via
  Ivy, which needs a Hadoop temp dir, which needs `winutils.exe`:
  `FileNotFoundException: HADOOP_HOME and hadoop.home.dir are unset`. The earlier
  smoke tests only *warned* because they never touched the filesystem. Do not
  chase a winutils binary -- run the job in the Airflow image, which is Linux and
  was built for exactly this.
- **Override `JAVA_HOME` and `PYSPARK_PYTHON` when passing `--env-file .env` to a
  container.** The file holds HOST values (`C:\jdk-...`), and the JVM then never
  starts: `Java gateway process exited before sending its port number`. Container
  values are `/usr/lib/jvm/java-17-openjdk-amd64` and
  `/home/airflow/venv/bin/python`.
- **`write_pandas` needs the connector's `[pandas]` extra**, not just pandas
  installed. The failure is a `MissingDependencyError` at write time, after the
  whole transformation has run.
- **`CREATE TABLE IF NOT EXISTS` is CREATE-only.** Adding a column to a table
  that already exists and re-running `apply_ddl.py` does nothing: no error, no
  change, and the omission surfaces later as `invalid identifier`. Schema
  evolution needs an explicit `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` at the
  bottom of `ddl.sql`.
- **Airflow task logs die with the container unless bound out.** The first
  compose mounted the project and nothing else, so a failed DAG run's logs were
  unrecoverable and `ingest_slips` showed only `attempt=3.log` -- no way to see
  what the first two attempts hit. `./airflow/logs:/opt/airflow/logs` fixes it.
  A pipeline whose failures cannot be read afterwards is not debuggable, and
  the logs are exactly what a reviewer asks to see.
- **dbt CONCATENATES a custom schema onto the profile's target.** Setting both
  `schema: ANALYTICS` in `profiles.yml` and `+schema: analytics` in
  `dbt_project.yml` creates `ANALYTICS_analytics`. Every model reports SUCCESS
  and none of them is where anything reads.
- **One `.env` cannot describe two network positions.** Host code reaches the
  databases at `localhost`; a container reaches them at `host.docker.internal`.
  Hence the `*_DB_URL_CONTAINER` copies that compose maps onto the plain names
  for the Airflow service -- the same split `arbibet-live`'s OPS.md uses.
- **Never pipe a gate through `tail`.** A pipeline's exit status is the LAST
  command's, so `mypy | tail -1 && pytest` reports success when mypy failed.
  A broken build was committed this way on the day CI was added, with three
  gates failing and the chain ignoring all three. Use `set -o pipefail`, or do
  not pipe.
- The Airflow image `arbibet-capstone-airflow:2.10.3` is **already built and verified**:
  JDK 17, dbt **1.12.3 stable** (dbt-snowflake 1.10.x pulls dbt-core 2.0.0-beta as a
  dependency, hence the `<1.10` pin), pyspark 4.2.0, all in a venv separate from
  Airflow's own so the scheduler's dependency set is untouched.

---

**`streamlit run` does not load `.env`.** The dashboard deliberately imports
no project code -- it is deployed to Streamlit Community Cloud from
`requirements.txt`, where the package does not exist -- so it cannot reach
`arbibet_capstone.env.load()`, and the first render died on
`SNOWFLAKE_ACCOUNT is not set`. It calls `load_dotenv()` itself. The rule that
entry points load `.env` and libraries never do still holds; this file is an
entry point that happens to live outside the package.

---

## 6b. Every timestamp is Europe/Berlin. Snowflake did not know that.

**The bug.** The deep-dive page showed *"Toulouse v Lille, kick-off Thursday 03
September 2026, 11:45"*. Ligue 1 does not kick off at 11:45. It is **20:45** in
Berlin.

**The cause, in two parts.**

1. **The session timezone was never set,** so Snowflake used its factory
   default, `America/Los_Angeles`. Berlin is UTC+2 in September, LA is UTC-7,
   so every wall clock printed **nine hours early**.
2. **`TIMESTAMP_TZ` ignores the session parameter anyway.** It stores the
   OFFSET ITS WRITER USED and renders in that offset forever. Every timestamp
   column in CORE is `TIMESTAMP_TZ`, and they were written while the session
   was still LA, so they carry `-07:00`. Setting the parameter fixes new
   writes and `TIMESTAMP_LTZ`; it does not touch a stored row.

**No instant was ever wrong.** `11:45-07:00` and `20:45+02:00` are the same
moment. The data was right and the display was wrong, which is why nothing
caught it: a chart's x-axis and its kick-off marker both came from the same
session, so they agreed with each other and the relative picture looked
perfect.

**The fix, in three places.** `warehouse.connect()` passes
`session_parameters={"TIMEZONE": "Europe/Berlin"}`; dbt sets the same in
`on-run-start`, because dbt opens its own sessions and the connector setting
does not reach them; and displayed columns are cast `::TIMESTAMP_LTZ` (the
`platform_time` macro, plus inline casts in the two dashboard views), which
re-renders a stored instant in session time. Re-writing the tables would also
work and is not worth it -- settle alone is 5.9M rows, and a stored offset is
not wrong, only local to its writer.

**Berlin, not UTC,** because markets bronze is the upstream source of truth for
`fire_time` and its own Postgres timezone is `Europe/Berlin`. Two zones in one
pipeline is how a chart's x-axis and its kick-off marker end up disagreeing.

**HOW THIS WAS MISSED.** Worth writing down, because the mechanism is the
recurring one in this project.

* Nothing failed. No exception, no test, no dbt assertion -- just a plausible
  number. An 11:45 kick-off is perfectly ordinary-looking unless you know that
  Ligue 1 plays in the evening. **Domain knowledge was the only detector, and
  the person with it was the user.**
* **The evidence had already been on screen and was not read.** An early
  diagnostic query printed
  `datetime.datetime(2026, 9, 2, 10, 30, tzinfo=pytz.FixedOffset(-420))`.
  `-420` minutes IS `-07:00`. It was looked straight at while chasing a
  different bug and dismissed as noise.
* **Two sources disagreeing was visible too.** bronze returned
  `ZoneInfo(key='Europe/Berlin')` in one query and Snowflake returned
  `FixedOffset(-420)` in another, in the same session. One pipeline printing
  two zones should have been the tell.
* And the default was never questioned: a warehouse's session timezone is the
  kind of setting that has to be chosen deliberately, and choosing nothing is
  choosing whatever the vendor picked.

**Rule: pin the session timezone when you open the connection, not when
someone notices.** And when a diagnostic prints an offset, read the offset.

---

## 7. The msport booking-slip API.

**Paging is a `lastId` cursor.** `pageNum`, `page`, `pageSize` and `currentPage` are all
silently ignored and return page one — which looks like a short list rather than a
mistake. `slips.list_codes` stops if a page repeats its predecessor, because fetching
the same twenty codes ten times would otherwise read as a successful deep fetch.

**The list refreshes every 10 minutes** (msport's own `rules` text) and is ranked by
popularity descending, so depth buys more than frequency. Ten pages is 200 slips. The
fetch budget is env-configurable and deliberately unambitious: this is a third party's
production API and there is no agreement with them.

**It is the slowest task in the DAG.** `ingest_slips` took 22 minutes of a
30-minute run and reached its third attempt. It is the only task hitting a
third party's production API, so the retries are plausibly rate limiting rather
than faults -- which would argue for a longer `retry_delay` rather than more
attempts. Unconfirmed: the earlier attempts' logs were lost to the missing
volume above.

**Legs disappear.** `bettableBetSlip` holds only still-bettable legs; a leg vanishes
when its match kicks off, and there is no history endpoint. Whatever is not captured
before kickoff is gone. The leg table can therefore only ever be append/upsert —
disappearance is a transition to "started", never a deletion.

**Store verbatim.** The Node-RED flow kept six fields per leg and discarded the rest.
The raw `event` object also carries `homeTeam`, `awayTeam`, `homeTeamId`, `awayTeamId`,
`tournament`, `category`, and — answering the lifecycle question directly — `status`,
`statusDescription`, `playedTime`, `remainTime`, `scoreOfWholeMatch`, `scoreOfSection`.
Parsing at ingest would have lost all of it, unrecoverably.

**Slip legs join the platform.** `eventId` is `sr:match:<id>`, exactly the id
`event_matches` uses (`msports;sr:match:73394630`). Market and outcome ids are the same
betradar namespace as the crosswalk. So a slip leg reaches the canonical fixture, its
odds history in markets bronze, and the settlement engine.

First fetch: 200 slips, follows 18–8,462, folds 1–41. Legs are queryable straight from
the VARIANT with `LATERAL FLATTEN` — no parsing job needed, which is the whole argument
for bronze-plus-dbt over hand-built fact tables.

---

## 8. Slip analysis already validated.

Multiplying msport's own per-leg `probability`:

| code | follows | legs | combined odds | P(win) | 1 in |
|---|---:|---:|---:|---:|---:|
| B2RY7A6 | 8,166 | 9 | 9 | 7.9e-02 | 13 |
| B2C32AC | 5,739 | 4 | 39,930 | 2.4e-06 | **411,956** |
| B2UWAC1 | 2,690 | 4 | 30,227 | 1.9e-06 | **529,183** |

Median 1 in 142; six of forty worse than 1 in 10,000. **Popularity and soundness are
uncorrelated** — that is the finding, and it is what makes "does this slip make sense?"
a real question rather than a gimmick.

Popularity, weighted by followers, on the first 200-slip fetch:

```
Celtic v Aberdeen          Premiership        138 slips   55,589 exposure
Falkirk FC v Rangers       Premiership        116 slips   48,988
Flamengo v Mirassol SP     Brasileiro Serie A 107 slips   47,774
```

Three caveats to state rather than hide:

- `bettableBetSlip` is the **remnant**, not the original slip (B2RY7A6 shows 9 of an
  original 48). Frame the metric as *copyability now* and the caveat disappears.
- Multiplying leg probabilities **assumes independence**. Fine across separate matches,
  wrong for two legs on the same match. Detecting same-event legs is both a correction
  and a soundness signal in its own right.
- `probability` is **msport's model**, embedding their margin. Still the best available
  prior, and it is what the punter was actually shown.

---

## 8b. Slip legs were duplicated, by two independent joins.

2,247 of the `(share_code, leg_index)` pairs in `gold_slip_leg_history` were
duplicated. Slips listed the same leg twice on the dashboard and, worse, the
LLM was handed leg-sets with repeats in them.

**Two causes, and they are the same mistake twice:**

1. **`bronze_slip_payload` is keyed `(source, share_code, payload_hash)`.** A
   slip re-fetched after losing a leg -- which is normal, legs vanish at
   kickoff -- is stored as a NEW ROW, not an update. 598 payload rows for 409
   slips. `stg_slip_leg` flattened all of them.
2. **`dim_fixture` is keyed on `event_id`, not `sr_match_id`,** and 12
   sr_match_ids resolve to more than one fixture row (the matcher can produce
   several events for one betradar match). Joining slips on `sr_match_id` fans
   every leg out once per matching row.

Both are: **reading a table as current-state when its key says otherwise.**
The identical shape appears in `gold_slip_summary_ai`, where a changed
`leg_signature` adds a row rather than replacing one -- caught separately when
the dashboard showed one slip twice with contradicting verdicts.

Fixed with two QUALIFYs in `stg_slip_leg`: newest payload per
`(source, share_code)`, and one fixture row per `sr_match_id` preferring the
one that actually resolved team ids (without them the leg reaches no settled
history, so a row that has them is strictly more useful). Pinned by
`assert_slip_legs_are_unique.sql`.

**The rule: before joining a table, check what its PRIMARY KEY actually is,
not what you are joining on.** Three separate bugs in this project have been
this.

---

## 8c. Three dashboard fixes, and a "1 in 1" that wasn't.

- **Rate column read 1% for 60%.** `st.column_config.ProgressColumn(...,
  format="%.0f%%")` applies a printf format to the raw 0-1 fraction, so `%.0f`
  of 0.60 rounds to "1%". The bar length was right; only the label was wrong.
  `format="percent"` is the token that reads a fraction as a percentage.

- **Deep dive opened a new window.** `st.link_button` is an anchor.
  `st.switch_page` keeps navigation in-tab -- but it CLEARS the query string,
  so `?event_id=` was lost and the fixture page showed "No fixture selected".
  The id is handed over in `st.session_state` and the fixture page restores it
  to the URL, so in-app clicks work AND the URL stays shareable.

- **A likely slip was called "extremely rare".** The bold opener showed the
  slip's combined implied probability as "1 in N", with N = round(combined
  odds). For an 84%-likely two-leg slip that is round(1.19) = 1, and the model,
  told to convey remoteness, wrote "1 in 1 ... extremely rare ... over 8,000
  daily slips" -- inventing the 8,000 from the `copied by` count. Fixed in the
  warehouse: `one_in_n` is NULL below combined odds 2.0, and the prompt frames
  those as a LIKELY outcome at the stated percentage instead. All the
  probability arithmetic is done in SQL and handed to the model finished; the
  system prompt forbids computing anything or reading the popularity count as a
  frequency.

The last one is the same lesson as every other confident-wrong bug here: the
number was plausible, nothing errored, and it took reading the rendered
sentence to catch it.

---

## 8d. Freshness: the arb/EV watcher (`watch/signals.py`).

**The problem.** `fact_arbitrage_signal` and `fact_ev_signal` were written only
by the batch consumers, which run only in the daily DAG. So the dashboard's
signals were as fresh as the last 06:00 run -- stale all day. The legacy
platform ran a local watcher that recomputed the instant new bronze prices
landed for a match; this restores that.

**Shape.** A long-running loop that polls markets bronze every
`WATCH_POLL_SECONDS` (20) for the newest write_time per upcoming fixture, and
recomputes any fixture whose prices moved since the last poll -- through the
SAME `snapshot.build` -> `arbitrage_rows`/`ev_rows` the consumers use, MERGEd on
`signal_key`. So the watcher and the DAG converge on the same rows; run both.
Its correctness rides on the consumers' code paths (and their tests); the
watcher itself is orchestration, untested by unit tests exactly as the
consumers are.

**Three guard rails, each learned the hard way in one sitting:**

1. **Pre-kickoff only.** Arb/EV matter only while the bet can be placed, and an
   in-play match is a payload storm. `kickoff > now` filters the watch set --
   correctness and load bound in one.
2. **A HOURS window, not days.** First run watched a 3-day window: 1,368
   fixtures, and `latest_write_times`'s `event_id = ANY(...)` over a
   1,300-element array became a seq scan of the 6.9M-row table -- **40 seconds
   a tick**. `WATCH_UNTIL_HOURS` (6) keeps the array to dozens, the index
   serves it in <1s, and markets barely reprices anything further out anyway.
3. **Prime, don't backfill.** With an empty cursor every priced fixture looks
   "changed", so the first tick tried to recompute ~1,300 fixtures the DAG had
   already covered. The first tick now just records the baseline; the watcher
   reacts to prices landing AFTER it starts. That is what a tail is.

**Snowflake stays cheap** because it is touched only to WRITE: a recompute that
finds no opportunity never runs a statement, so a quiet hour resumes nothing.
`bookmaker_ids` at startup is the one unconditional query. Verified live: new
signals landed within one poll, `detected_at` in Berlin time, arb 25 -> 26.

**Not a DAG task.** It is a persistent process (`python watch/signals.py`), the
freshness path; the DAG remains the completeness/backfill path. In a real
deployment the odds path runs on a cadence like this and only the history path
is daily.

---

## 8e. The DAG failed twice, for two reasons, and the second had been "succeeding" for days.

**Symptom.** Every run after 08:29 on 3 Sep failed at `producer`; everything
else was `upstream_failed`. Two root causes, found in order.

**1. `MSG_SIZE_TOO_LARGE` -- a fixture snapshot over 1,000,000 bytes.**
Real Sociedad v Celta Vigo: 5 books, 1,624 markets, 1,150,521 bytes of JSON.
librdkafka enforces `message.max.bytes` (default 1,000,000) CLIENT-SIDE by
raising from `produce()`, and nothing in the producer caught it, so one big
fixture took the whole daily run down. Toulouse v Lille was 903 KB -- one busy
day from the same failure. Data-dependent, which is why earlier runs passed.

Fixed on all three sides at once, because a ceiling on one side just moves the
failure somewhere quieter: producer `compression.type=gzip` (the JSON
compresses ~11x; worst case became ~101 KB) and `message.max.bytes` = 8 MiB;
topic `max.message.bytes` and cluster `kafka_batch_max_bytes` = 8 MiB (the
cluster value persisted in compose); consumers `max.partition.fetch.bytes`
= 8 MiB. And `produce()` is now isolated per fixture: an oversize message is
logged with its size and fixture, counted as a failure, and does not stop the
run.

**2. Redpanda advertised `localhost:9092`, so every container-side client
connected to itself.** Redpanda returns its ADVERTISED address in metadata
and clients reconnect to that. A single listener advertised as localhost was
chosen so host tooling would work -- and the compose comment even said
"nothing in this project connects to Kafka from inside a container". The
Airflow tasks do. Inside the container the producer bootstrapped via
`redpanda:9092`, was told the leader lived at `localhost:9092`, and was
refused every 30 s for seven minutes until `message.timeout.ms` expired.

**Then it logged `published=73 skipped=8 failed=0` and exited 0.** Seventy-three
messages that never reached the topic, reported as success. `produce()` only
queues; delivery is reported asynchronously, and the producer ignored both the
delivery reports and `flush()`'s return value (the count still outstanding).
This is the silent-wrongness pattern in its purest form, and it means the
"successful" 08:29 run almost certainly published into the void as well.

Fixed with two listeners -- `internal://redpanda:9092` advertised to containers,
`external://localhost:19092` advertised to the host -- and **the host bootstrap
is now `localhost:19092`** in `.env`. The producer counts delivery failures and
the flush remainder as failures, subtracts them from `published`, and exits
non-zero. Verified: host `list_topics` sees `localhost:19092`, the container
sees `redpanda:9092`.

**Then the rerun found a fourth thing.** With the producer delivering, the
consumers wrote 12 new arbitrage rows -- and `dbt_test` failed: one signal's
`market_base_id` (60210) had no row in `dim_market`. The orphan was worse than
a missing dimension: **both legs were sportybet**. sportybet alone priced that
two-way market at 1.15 and 7.10; `1/1.15 + 1/7.10 = 1.011`, "arbitrage 0.9897",
and the 0.98 near-arbitrage threshold let it in. The engine's only guard was
at snapshot level (two books present), not per market. Two outcomes of one
book are that book's own overround, not a cross-book disagreement -- and it
was polluting `gold_market_efficiency` with exactly the single-book vig its
docstring says it does not measure. Fixed: `arbitrage_rows` now requires the
legs to span two distinct bookmakers; pinned in `test_signals`; the one such
row in the warehouse deleted (37 signals remain, all 4 surebets untouched).

And the `relationships` tests on `market_base_id` were encoding a false
assumption: `dim_market` is the crosswalk, and native books legitimately price
markets outside it. Both tests are now warn-severity, with the reason inline.

**3. The logs bind mount was declared but not applied.** `./airflow/logs` was
added to compose, but the running container predated it and had only
`/opt/project` mounted -- so a failed task's logs were still dying with the
container, and the host directory stayed empty. A compose edit is not a
running container. `docker compose up -d airflow` recreates it, and it did.

**5. The UI died in the recreate and stayed dead.** `airflow standalone` runs
the webserver as a child and does NOT respawn it. During the container
recreate, gunicorn's master got no response within the default 120 s
`web_server_master_timeout` -- `db migrate`, the scheduler and four sync
workers each importing the entire providers tree were all starting at once --
so Airflow logged "Shutting down webserver" at 19:12:47 and the pipeline ran on
for eight hours with nothing on port 8080. Compose now sets
`AIRFLOW__WEBSERVER__WORKERS=2`, `WEB_SERVER_MASTER_TIMEOUT=600` and
`WEB_SERVER_WORKER_TIMEOUT=300`. If 8080 ever stops answering, check the
container log for that line before anything else; the fix is a recreate.

**Also.** Redpanda has no volume here; recreating it drops the topic and every
consumer group. Recreate the topic and re-apply `max.message.bytes` after any
redpanda recreate. Standalone Airflow takes ~3 minutes after a recreate before
the scheduler picks anything up; a run that sits `queued` for that long is
not stuck.

---

## 8f. A cached Snowflake connection expires. Two callers hold one for hours.

`390114 (08001): Authentication token has expired` on every query, after the
dashboard had been open overnight.

A Snowflake session's token lapses after roughly four IDLE hours. The dashboard
caches one connection with `st.cache_resource`, which lives as long as the
Streamlit process -- fourteen hours, in this case -- and nothing renewed or
replaced it. The page was permanently broken; only a restart fixed it.

Two fixes, because prevention and recovery are different jobs:

* **`client_session_keep_alive=True`** on both `warehouse.connect()` and the
  dashboard's connection. It heartbeats the session so the token renews
  instead of lapsing.
* **Retry once on a dead session.** `query()` catches the expiry, calls
  `connection.clear()` to drop the cached object, and re-runs. Keep-alive
  cannot survive a slept laptop, a dropped network, or a session killed
  server-side, and without a retry any of those is a permanently broken page.
  `st.cache_data` caches results and not exceptions, so a failed attempt
  leaves nothing stale behind.

**The same trap was latent in `watch/signals.py`**, which holds one connection
for its whole run and deliberately goes quiet whenever there is no signal to
write -- so it would have failed on the first opportunity it found after a
quiet night, which is exactly the moment it must not. Fixed by the same
keep-alive. Short-lived batch jobs finish long before this matters and the
heartbeat costs them nothing.

**The general shape:** any connection cached for the life of a long-running
process needs a keep-alive AND a reconnect path. Caching the connection was
right; assuming it stays valid forever was not.

---

## 9. Conventions in force.

- **KISS / YAGNI / DRY / SOLID**, applied explicitly and out loud. Abstractions arrive
  when a second caller does, not before: `db.py` was extracted only when the matcher
  read appeared, having been named in advance as the trigger.
- **Pure logic split from I/O**, so tests are real rather than mock theatre:
  `build`/`fetch`, `_merge_sql`/`merge`, `arbitrage_rows`, `list_codes`.
- **Every write is a MERGE on a natural key.** Snowflake enforces NOT NULL and nothing
  else; PRIMARY KEY is documentation. Columns omitted from a row are never updated,
  which is how `detected_at` and `first_fetched_at` keep their original values.
- **Failures are loud.** No blanket `try/except` around a parser. Isolation belongs at
  the fixture or message level, where a skip is logged with its traceback — never
  folded into an empty result that reads as "no markets".
- `crosswalk/` is vendored and excluded from ruff and mypy. Typing it would mean
  rewriting it, and not rewriting it is the point of vendoring.
- **After changing a parser, reset the topic before measuring anything.** The
  messages on `market.ticks` were parsed by the code that was running when they
  were produced. Measuring a parser fix against them shows the OLD behaviour,
  which misled three separate investigations in one session. The sequence is:
  change parser, `rpk topic delete` + `create`, delete the consumer groups,
  re-run the producer, then measure.
- **Two sessions in one working tree will commit each other's files.** Both run
  `git add -A`, so work in flight gets swept into whichever commit lands first,
  and history attributes it to the wrong change. Use a `git worktree` per
  session, or accept that commit boundaries are approximate.
- **Test fixtures must be realistic, or they silently remove a branch from
  coverage.** The 1X2 fixture had two outcomes -- home and away, no draw --
  because two probabilities summing to 1.0 sail through the compactible
  filter. The cost was not cosmetic: with outcomes <= books,
  `assign_unique_bookmakers_for_market` never has anything to reassign, so
  nothing in the suite reached the branch that exists for exactly that case.
  A fixture that could not occur in production tests a program that does not
  exist.
- Every step ends with `pytest`, `ruff check`, `mypy` **and a run against live data**.
  Three of the four defects in section 1 were found by the live run, not by a test.

---

## 10. The settlement engine: two caller obligations it will not guess at.

Both were found by the engine **refusing**, which is the refusal design paying
for itself -- neither produced a wrong verdict, and neither would have been
visible in a test.

**a. The status vocabulary is silver's, not API-Football's.** `settle()` expects
`ft` / `aet` / `pen`; the payloads carry `FT` / `AET` / `PEN`. Mismatched, it
refuses every row with `unknown_status` -- 87,600 of them on the first run. Map
explicitly (`STATUS_TO_ENGINE` in `spark/settle.py`) rather than with `.lower()`:
the two agreeing in case is a coincidence for exactly these three tokens, and
`ABD` is `abandoned`, `NS` is `not_started`.

**b. `handicap` does not take a decimal line.** It uses betradar's goal-handicap
notation -- `0:1`, `1:0` -- a start-of-match scoreline. A shared `2.5`-style
ladder earns `line_invalid` on every one of its rows. `asian_handicap` *does*
take decimals. Lines live in `LINES_BY_FAMILY`, per family, because the maps
declare that a family HAS a line key and never which lines exist.

**The catalogue is derived, not written.** `market_map.lookup()` gives family,
period and the rule that fixes the time basis; `outcome_map.lookup()` gives the
sides and whether a line applies. Restricted to `ARB_RELEVANT_MARKETS`, since
settling all 148 mapped ids would describe markets none of these books quote.

---

## 11. `side_or_line = 'home'` means "this team", not "the home team".

The single most important thing to know before writing a form query.

`team_perspective` mirrors the verdict for directional families, so a team's row
says whether that side resolved **in that team's favour**. Verified on
Tottenham 1-0 Everton:

| side | Tottenham (home, won) | Everton (away, lost) |
|---|---|---|
| `home` | won | lost |
| `draw` | lost | lost |
| `btts` `no` | won | won |
| `over@0.5` | won | won |

So `WHERE market_family='1x2' AND side_or_line='home' AND team_id = X` counts
**X's wins, home or away**. Symmetric families (`btts`, `total_goals`) carry the
same verdict for both teams, which is correct -- they are facts about the
fixture that both sides observe.

A form query that filters fixture-level sides without accounting for this will
count the opponent's results as the team's own.

`team_scoped_not_applicable` refusals are expected and correct: families like
"home clean sheet" apply to one team, so the other team's row is refused rather
than invented.

### The consequence: two teams' histories may not always be pooled.

This is the mistake a later session is most likely to make, because the wrong
version produces a plausible number rather than an error.

`gold_slip_leg_history` first averaged both sides' records for every family.
On a real slip that gave **Osnabruck to beat Bayern Munich at 38.89, "historical
rate 0.700"** -- because Bayern win 9 of 10 and their record was folded in as
though it supported an Osnabruck win. It is evidence *against* the leg.

The rule, which `dim_market_outcome.classification` now carries:

| classification | pooling |
|---|---|
| `symmetric` (`total_goals`, `btts`, `odd_even`) | **Pool.** Both teams observe one fact: over 2.5 happened, or it did not. |
| `directional` (`1x2`, `double_chance`, `draw_no_bet`, handicaps) | **Never pool.** Use only the side the leg names -- `away%` sides take the away team's row, everything else the home team's. |
| `team_scoped_*` | One team only, by definition. |

And a caveat that belongs beside any use of these rates: `historical_rate`
knows nothing about opponent strength. Osnabruck's 5-in-10 is against their
usual opposition, not against Bayern, and ten matches move ten points on one
result. It is recent form. It is not a probability for this fixture, and
nothing -- the summariser above all -- may present it as one.

---

## 12. Slips and settlement compose. Nobody has wired them together yet.

The two halves were built in separate sessions and meet cleanly:

- A slip leg carries `marketId` and `outcome.id` in **the same betradar
  namespace** the settlement catalogue is built from, so a leg maps onto a
  `(market_family, period, side_or_line)` through the same two maps.
- A slip leg's `eventId` is `sr:match:<id>`, which `event_matches` uses, which
  reaches `apifootball_events.home_id` / `away_id`, which is what
  `fact_team_market_result.team_id` is keyed on.
- **But the slip's own team ids are useless for this.** A leg carries
  `sr:competitor:5981`, and that id appears NOWHERE else in this warehouse --
  the history facts are keyed on API-Football ids. The fixture is the only
  bridge, which is why `sr_match_id` now sits on `dim_fixture`. Every book's
  `e_id` carries the same one (`msports;sr:match:X`, `sportybet;sr:match:X`,
  `ilobet;sr:match:X`), so it identifies the fixture rather than a book's view
  of it.
- The betradar maps had to be materialised as `dim_market_outcome` (754 rows)
  before any of this was reachable from SQL. They are Python data; without a
  table, only Python can resolve a slip leg's market.

So "does this slip make sense?" stops being a probability multiplication and
becomes a grounded answer: *this leg is Over 2.5; these two sides have gone over
in 7 of their last 10; the book prices it at 1.82.* That is the strongest thing
this project can say, and it needs no new source -- only a join.

The independence caveat in section 8 gets sharper too: two legs on the same
fixture are detectable as identical `fixture_id`, and their correlation is
measurable from `fact_team_market_result` rather than assumed.

---

## 13. Two behaviours a reader will otherwise assume wrongly.

**`assign_unique_bookmakers_for_market` is not always unique.** It spreads legs
across books to maximise what is placeable, but with fewer books than outcomes
one book must take two, and its documented fallback keeps the original
assignment rather than dropping an outcome. A three-way market priced by two
books returns legs like `msport / sportybet / sportybet`. Two bets at one
bookmaker are still placeable, so this limits the optimisation and not the
signal -- but anyone reading a three-leg row as "three different accounts" is
wrong.

**`gold_market_efficiency` is not per-bookmaker vig, and cannot be.** The plan
asked for weekly vig by book; a book's margin needs that book's COMPLETE
market, and `fact_arbitrage_signal` stores the best price per outcome ACROSS
books. What the model reports is the overround left after shopping every book,
`1 / arbitrage - 1`: zero means the best prices exactly cancel, negative is a
true surebet, positive is what a punter still pays at the best available
prices. For a cross-bookmaker platform that is the more relevant number, but it
answers a different question and the column names say so.

Reported twice, whole and fresh-only, because the gap between those two columns
is the cost of stale legs -- see section 3.

---

## 12b. Publishing rules for the dashboard.

**Bookmaker masking was built and then REVERTED, on request.** The dashboard
names sportybet, msport, ilotbet, bet9ja and livescorebet directly again.

Keeping the reasoning, because re-applying it is a live option and the traps
are not obvious:

* The mapping belongs in `dim_bookmaker`, not the dashboard, so every panel
  agrees. A pseudonym invented at the point of display drifts between panels.
* `p_source` on an EV signal holds a bookmaker NAME. Masking `bookmaker_name`
  and leaving it identifies a book by the back door -- and specifically one of
  the two that publish a probability, which is a much smaller set to guess
  from.
* `is_own_probability` must stay computed from the REAL names. It is a fact
  about the signal and must not change with how a book is labelled.
* Captions leak too: "Only sportybet and msport publish a probability" had to
  become "only two of the five books".
* It was never secrecy. The repo names every book in its README, its FINDINGS
  and its parser filenames.

**Font size lives in `.streamlit/config.toml`** (`baseFontSize = 18`), which is
committed, because Streamlit Community Cloud reads the theme from the repo. A
theme set only on the developer's machine is a theme the published page does
not have.

Setting `baseFontSize` does NOT stop the page following the viewer's dark/light
preference -- an earlier note here said it did, and the screenshot capture
disproved it: the same page rendered dark under a headless browser reporting a
dark preference and light under `color_scheme="light"`. Because the page really
does follow the viewer, a capture has to CHOOSE, and
`docs/capture_screenshots.py` chooses light: the images are embedded in a
README that GitHub renders on white.

**Every panel is dated.** Bronze prunes, the fixture window moves, the producer
runs again, and the page is cached for ten minutes on top of that. An undated
page silently claims to be current.

**Every fixture join is LEFT, falling back to `event_id`.** Signals outlive the
fixture rows they point at, and a detected opportunity must not vanish from the
page because its dimension aged out of the loader window.

**Near-arbitrage is not shown as a headline.** The consumer records down to
0.98 so market efficiency can be measured -- that needs the markets that did
NOT quite get there -- but a headline counting them reads as "25
opportunities" when there were four. The efficiency chart is the one panel that
uses them, and says so.

**`leg_spread_seconds` is displayed as "spread", never "lag".** Lag suggests a
delay between us and the market. This is disagreement between the BOOKS' own
clocks: the gap between the oldest and newest price in the set. Nothing is
filtered on it, wide ones included -- filtering would flatter the result.

---

## 12c. Outcome ids are market-scoped. Get the name from the book.

`dim_market_outcome` is materialised from the settlement engine's maps, and the
engine settles from SCORES. Markets it cannot settle -- corners above all --
are absent from it entirely. Both of the best surebets found are on first-half
corners, so the most prominent table on the dashboard displayed `outcome 12`
and `outcome 13`.

**Resolving the id alone is not the fix.** Across the taxonomy, outcome id 12
has two distinct meanings (`over` and `1-2`) and id 13 has three. A global
id-to-name map would print a confident, plausible, wrong label -- the exact
failure mode this project keeps rediscovering.

**The books already publish the name.** Every parser carries `Outcome.name`
through from the payload; it was simply being dropped. `fact_odds_tick` now
stores it, and `stg_outcome_label` picks the most frequently published name per
`(market_id, outcome_id)`. The label is the bookmakers' own word for the
outcome rather than anyone's inference, and `outcome 12` became `Over 5.5`.

Resolution order, most authoritative first: the settlement taxonomy, then the
observed name, then the raw id. The raw-id fallback stays -- a market nobody
has ever been seen pricing genuinely has no name we can honestly give it.

---

## 13a. `apifootball_events` is a rolling window, and the MERGE was erasing the warehouse with it.

The worst bug in the project, found on the day it was due, by looking at a
dashboard.

**What it looked like.** Six slip cards, each reading `0 with history`, each
with a summary written a day earlier citing form of 90% and 75%. Nothing had
errored. No test had failed. Row counts were unchanged.

**The chain.**

1. `dim_fixture.home_team_id` comes from `apifootball_events`, reached through
   an `apifootball;<id>` leg in the matcher's `bookmaker_event_ids`.
2. **`apifootball_events` is a rolling window.** It held ids around 1,635,000
   while the fixtures behind live booking slips needed 1,525,921 and 1,550,705
   -- aged out weeks earlier.
3. The leg still exists, so the LEFT JOIN still matches and still returns a
   row. It just returns NULL team ids.
4. `_merge_into` wrote `t.home_team_id = s.home_team_id` unconditionally. Every
   `load_dims` run therefore **overwrote ids the warehouse already held with
   NULLs from a source that had simply forgotten them.** 3,226 fixtures blanked.
5. No team id means no join to `fact_team_market_result`, so every slip leg on
   those fixtures lost its settled history -- the join this whole project was
   built to demonstrate, silently down from 86% to 49% of legs.

**The rule.** *A NULL from the source means "not known on this read", never
"known to be nothing".* `merge_bulk` now takes `keep=[...]`, and those columns
update to `COALESCE(s.c, t.c)`. `dim_fixture` keeps team ids, apifootball id,
sr id, names and tournament. **`kickoff_at` is deliberately NOT kept** -- a
postponement is real news and must be able to win.

**Recovering what was lost.** Two things outlive the window: the matcher keeps
`apifootball;<id>` in `bookmaker_event_ids`, and `fact_team_match` holds
149,120 fixtures from the ingestor's own bronze. So `fixtures.py` now reads the
id from the LEG (`COALESCE(a.fixture_id, split_part(af.e_id, ';', 2))`) rather
than the projection, and `load_dims` ends with a backfill joining
`fact_team_match` on `apifootball_id`, filling NULLs only. **2,295 of 3,226
fixtures recovered**; legs with history went 49% -> 86%, and the remaining 931
are fixtures API-Football never covered.

**Why this took a dashboard to find.** Every guard in the project was pointed
at values: NOT NULL constraints, dbt tests, `assert_gold_is_not_empty`. This
bug produced no bad value and no missing row -- it produced a NULL where a
number used to be, in a nullable column, on a row that still existed. The only
artefact that could show it was one that put a claim and its evidence side by
side and let a human notice they disagreed.

Anything reading a rolling or windowed source deserves the same suspicion.

---

## 13b. A cache key must cover the evidence, not just the subject.

`gold_slip_summary_ai` skips a slip whose `leg_signature` it already holds, and
that signature hashed each leg's fixture, market, pick and odds. It did not
hash the form numbers.

The dashboard put the summary next to the leg table for the first time, and the
top card read:

> "The Osnabruck v Bayern Munich leg stands out with an implied chance of 98%
> and **recent form of 90%**, indicating a solid track record."

beside a table whose every row said **`no history`**. Legs byte-identical,
signature unchanged, summary therefore "current" and never rewritten -- while
the evidence underneath it had gone to NULL.

The evidence moves independently of the legs, which is the whole point:

* A fixture resolves to API-Football team ids through the matcher, and that
  resolution is partial and not stable -- `dim_fixture` carries team ids for
  1,521 of 4,747 fixtures. A leg that had form yesterday can have none today
  without one price changing.
* Settling more history changes a rate without touching a leg at all.

So `Slip.signature()` now hashes `wins/matches` alongside the prices. Any
summary whose evidence shifted is regenerated; one whose evidence is stable
still costs nothing. B2RY7A6 rewritten now reads *"both of which have form:
none, indicating there is no settled history"* -- the honest answer, and the
one the prompt was always instructed to give.

**The general rule: if a cached artefact describes X, X belongs in its cache
key.** Hashing only the thing being described lets the description outlive it.
`tests/test_summarise.py::test_the_signature_changes_when_only_the_evidence_does`
pins it.

**And the reason it was caught at all:** the panel shows the model's input
beside the model's output. Every other display of this summary -- the table,
an API, a slide -- would have shown a confident sentence with nothing to
contradict it. Showing the evidence next to the claim is a correctness
feature, not a design flourish.

---

## 13c. The result note said "away won" about a 0-0. Three joins, three wrong answers.

`enrich/result_summary.py` shipped its first four notes and two were false. The
model wrote "a signal was detected, and the outcome it pointed at came in" about
a fixture with none, and "the away and draw markets won" about a 0-0 draw. The
instinct was to blame the model. All three causes were mine.

**1. A team-relative verdict read as a fixture-level one.** `_MARKETS` selected
`fact_team_market_result.verdict` with `is_home = TRUE` and handed it over as if
`away -> won` meant the away side had won. It does not. §11 and
`team_perspective.py` both say so: the resolver mirrors the **verdict** and
leaves `side_or_line` at its fixture-level value, so on the home team's row
`away` carries the away side's result *as it affected this team*. Across 400
fixtures the `away` column was inverted in all three outcomes -- W, L and D --
with no exceptions, which is what a systematic sign error looks like next to a
genuine bug's ragged edges.

The fix does not restate the rule. `team_perspective` mirrors `won <-> lost`
with push and void self-mirroring, which makes it an **involution**: calling it
a second time with the same team returns the fixture-level verdict. So the
module that declared the mirror is asked to undo it. A second copy of that
table in SQL is exactly what its docstring forbids.

**2. A price join on `event_id` alone.** `max(t.odds)` over every tick in the
fixture returns the longest price *anywhere in it*, identically, on every line:
`25.0` printed as the price of a 1x2 home, a BTTS yes and a double chance, on
the same fixture. `dim_market_outcome` exists precisely to bridge the settlement
taxonomy to `(market_id, outcome_id)`, and joining through it gives 1x2 home
1.78 beside a monotonic goals ladder 1.06 / 1.30 / 1.91 / 3.25 / 6.34. The wrong
version never errored and every number in it was a real price.

**3. An absence the model was trusted to notice.** With no signals the prompt
simply had no `signal` lines, and the instruction read "If a signal was
detected, say whether...". A conditional over an invisible antecedent is an
invitation. Absences now print: `signal | NONE`.

### The tail: forbidding a word is not fixing the pressure

With all three fixed, one note still said "all total goals markets lost" where
`under 0.5` was printed as **won** two lines above. Adding "never write `all` or
`every` about a group of markets" to the prompt fixed three fixtures and not the
fourth, which switched to "all ... except over 3.5 and over 4.5 won" and swept
`under 0.5` into it.

The pressure came from the input. Six of fourteen lines were a goals ladder
differing only in a number, and a model asked for three sentences will compress
them. `_thin()` now keeps at most three lines per family and **takes one of each
distinct verdict first**, so the contrast inside a family is always visible and
a family that is genuinely unanimous stays unanimous -- there is then no false
generalisation available to make. All four notes came back fully enumerated and
correct. The prompt rule stays as a backstop; it was not the fix.

The pattern is §12b's again, three times over: every one of these produced a
fluent, plausible, checkable-looking sentence rather than an error.

## 13d. A threshold on change cannot be built by rounding.

The half-hourly DAG needs briefs that refresh when something happens and stay
put when nothing does. The old test was a hash of the evidence, which included
the running COUNT of price changes -- so every fixture was rewritten on every
run in which any book twitched. Forty-eight LLM calls a day per fixture to move
"1.85" to "1.90" in one sentence.

The obvious fix is to round each number to the threshold and hash that: no
stored state, one line of code. **It does not work, and it reports success.** A
fixed lattice of width `w` turns a move of size `d` into a rewrite with
probability `d/w`, because what matters is not the size of the move but whether
it happens to straddle a boundary. Measured on our own 156,171 repricings,
banding implied probability to 0.02 leaves **46.7%** of moves in the same band
-- so more than half of all ticks still trigger a rewrite, most of them
movements of a fifth of a point of probability, while the log says the
thresholds are working.

So the previous brief's numbers are stored (`gold_fixture_summary_ai.evidence`,
a VARIANT) and the next run compares against them. A threshold on change has to
be a comparison; there is no stateless version of it.

**The numbers, and the guess as to why they sit there.** Across those 156,171
repricings the median move is 0.009 of implied probability and 79.6% are under
0.02 (p75 0.0172, p90 0.0346). Books quote on a discrete ladder whose step near
even money is 0.05 of decimal odds -- 1.85 to 1.90 is 1.4 points of probability
-- so most recorded "moves" are one rung. `PRICE_BAND = 0.02` sits just above
one rung: it ignores a book shading a price and fires on a book changing its
mind. `ARB_STEP = 0.01`, because 45.7% of arbitrage re-detections that moved at
all moved less than that, and real arbitrage lives in 1.00-1.02 -- one point of
ratio is most of the distance between "worth the stake" and "gone once
commission is paid".

Not thresholded, at any size: a signal appearing or disappearing, a change in
leg count or in which books carry the legs, and any change to form or settled
rates. Those are changes of state, and a threshold that swallowed one would
leave the page saying an arbitrage is still there after it has gone.

Verified both ways: 1.85 -> 1.90 does not rewrite, 1.85 -> 2.00 does; arbitrage
+0.003 does not, +0.012 does; a leg moving to a different book always does. Two
consecutive real runs went `rewriting=4` then `rewriting=0`.

## 13e. A consumer that reports zero against a full topic.

`consumers/arb.py` logged `messages=0 rows_written=0 failed=0` and exited 0.
Rerun minutes later, unchanged, it read **934 messages and wrote 138 rows**.

`IDLE_EXIT_SECONDS` is 10 and the poll timeout is 1s, so ten empty polls end the
run. The code already carried a comment about this -- the idle clock was moved
to start AFTER the Snowflake handshake for exactly this reason -- but that only
covered the handshake. Joining a Kafka consumer group is a separate wait: when
the previous member has left, the coordinator rebalances, and that can take most
of `session.timeout.ms` (45s by default), which is four times the idle window.

The clock now does not start until `consumer.assignment()` is non-empty, bounded
separately by `JOIN_WAIT_SECONDS` (90) after which the run FAILS rather than
reporting an empty read. Both consumers were patched; both were re-run and
exited cleanly in 13s once assigned.

This one matters more than its size. On a half-hourly schedule it is a green
Airflow task that silently does nothing -- and it is the same shape as every
other bug here: not an error, a plausible number.

## 13f. The headline number was 94% artefact.

"True surebets: 139" was the first number on the dashboard. **131 of those 139
were detected after their fixture had kicked off**, and the largest of them
reads 1.6788 -- a 68% guaranteed return. Eight were found before kick-off, the
best of them 1.0396, which is what a real one looks like.

The cause is §12b's: in-play, books suspend and resume at different moments, so
a frozen price sits beside a live one and the arithmetic is faultless on prices
that were never simultaneously available. The producer now drops fixtures that
have already started, which stops new ones being made. This is the other half
-- what to do with the 131 already recorded.

They are kept, because they measure something real: how far books drift out of
step in-play. But they are out of the count, out of the table, and named on the
page for what they are. The headline now reads 8 and 115 rather than 139 and
320, and the surebet tab carries the excluded count and the reason above it.

Deleting them was the other option and would have been worse: the evidence for
the pre-kick-off rule IS those 131 rows, and a project whose thesis is "every
serious bug here produced a plausible number rather than an error" should not
delete its best example of one.

## 13g. It was never about kick-off. It was the gap between the legs.

§13f blamed phantom arbitrage on signals detected after kick-off, and the
producer's pre-kick-off filter and the dashboard's exclusion notice were both
built on that. **The reasoning was wrong, and it was wrong because I read the
wrong timestamp.** `detected_at` is when the CONSUMER ran, not when the prices
were observed. A snapshot published at kick-off minus two minutes and consumed
ten minutes later stamps `detected_at` after kick-off while every price in it
is pre-match.

Checked against the price times instead (`oldest_leg_fire_time` /
`newest_leg_fire_time`), of the 48 rows failing
`assert_arbitrage_is_physically_plausible`, **35 have every leg priced before
kick-off**. The thing they share is not kick-off. It is
`leg_spread_seconds`:

    leg spread        signals  surebets  implausible  worst
    within 1 min          177        14            0  1.054
    1-5 min                17         7            0  1.059
    5-30 min               64        36           11  1.309
    30 min - 2 h          125        63           21  1.679
    over 2 h               66        25           16  1.588

**Not one implausible signal has legs inside five minutes**, across 194 signals,
and the worst arbitrage in that band is 1.054. Every one of the 48 is above it.

The worst signal in the warehouse reads 1.6788 -- a 68% guaranteed return -- on
a first-half 1X2 whose legs were priced 78 minutes apart:

    draw  sportybet   7.10   implied 0.141
    away  msport     70.00   implied 0.014
    home  bet9ja      2.27   implied 0.441
                             ---------------
                             sums to 0.596

A real market's implied probabilities always sum ABOVE 1.0; the excess is the
book's margin. Summing to 0.596 is not an edge, it is proof the three prices
were never on sale at the same time. `away` at 70.0 is a dead price left
standing on a market that had stopped trading.

**The mechanism.** A snapshot carries each book's LAST SEEN price per outcome,
with no liveness check. A book that reprices every minute and a book that
stopped publishing an hour ago contribute equally, and the detector combines
them. Arbitrage is a claim that three prices are simultaneously buyable; this
one only checks that they are simultaneously *recorded*.

**Why the kick-off filter looked like it worked.** It does help, by accident: a
signal detected before kick-off had its consumer run close behind its producer,
so its legs are necessarily tight -- median spread 2.9 minutes for those, against
71.3 for the ones it excludes. Right answer, wrong reason, and a rule that holds
for a reason you have misidentified will break the first time the reason
changes.

**The fix this points at** is a freshness constraint on the legs, at the
detector, not a filter on kick-off downstream. The data puts the bar at five
minutes. §13f's exclusion notice on the dashboard should say leg spread, not
kick-off, and the pre-kick-off producer filter should stay -- it is independently
right, since a bet on a started match cannot be placed.

### 13g, implemented (2026-09-15)

* `arbitrage_rows` refuses any set whose legs span more than
  `MAX_LEG_SPREAD_SECONDS = 300` (env `ARB_MAX_LEG_SPREAD_SECONDS`; dbt var
  `max_leg_spread_seconds`). `is_surebet` in both staging models requires it.
* `detected_at` is now MARKET time -- the newest leg's `fire_time` for
  arbitrage, `payload_fire_time` for EV. The consumer's clock moved to a new
  `consumed_at`. All 454 arbitrage and 337 EV rows were backfilled, their old
  `detected_at` copied into `consumed_at` first. The median gap it had been
  hiding was **11 hours** for arbitrage and 85 minutes for EV.
* `assert_arbitrage_is_physically_plausible` is scoped to fresh signals, and
  `assert_no_stale_arbitrage_detected_after_freshness_rule` guards that the
  excluded set stays history rather than quietly growing.
* First signals run afterwards: `dbt_test` passed for the first time since
  09-07; its 2 new arbitrage rows had a spread of 0s.

## 13h. "Slips are not updating." They were. Their verdicts were not.

Ingestion never stopped: on 09-15 bronze had been fetched that morning and
`gold_slip_leg_history` held legs kicking off on 09-17. What froze was
`gold_slip_summary_ai`, at 09-06 23:15 -- because `summarise` runs after
`dbt_test`, which failed on every run from 09-07 (§13g). The dashboard only
draws slips that have a verdict, so a blocked test looked like a dead feed.

Unblocking it would not have been enough. `summarise` ranked ALL pending slips
by copies, and copies accumulate over time, so the most-copied slips are always
old ones: on 09-15 there were 166 upcoming slips and **not one** had a verdict.
The budget is now split between upcoming and played, each most-copied first,
with whatever one side leaves going to the other. The dashboard splits the same
way -- Popular upcoming / Popular played -- and so do the signal tabs and the
deep dives, for the same reason: one ranking over past and future is a ranking
of the past.

"Upcoming" is judged at render time in `dashboard/common.py::is_upcoming`, not
in SQL. Queries are cached until the warehouse is next written, so a SQL-side
`kickoff_at > current_timestamp()` would leave a kicked-off fixture listed as
upcoming for up to a whole DAG interval.

## 13i. EV was judged against a fixed-priority probability, however old.

Only sportybet and msport publish a probability, so every EV is a price at one
book measured against a probability from one of those two. Which one was a
fixed priority -- sportybet, then msport -- with no regard to age: a sportybet
number from two hours ago beat an msport number from two minutes ago. And
nothing checked that the price and the probability were current together,
which is §13g's stale-leg defect in a one-leg costume.

Now:

* The probability is the MOST RECENT of the two, by payload `fire_time` -- the
  moment the book last confirmed it, not the per-outcome `lastChange`, since
  unchanged is not stale. An exact tie goes to sportybet, so the choice is
  deterministic.
* It must be within 300s of the price (`EV_MAX_PROBABILITY_SPREAD_SECONDS`),
  or the row is refused. The bar is borrowed from arbitrage, where it was
  measured; EV never recorded the probability's age, so there was nothing to
  measure it on. `probability_fire_time` and `probability_spread_seconds` are
  stored from now on, so the next version of this bar can be measured.
* `detected_at` is the later of the two times -- the first moment both existed.
* `stg_ev_signal.is_fresh` is NULL, not FALSE, on the 374 rows written before
  this: their probability's age is unknown, not known to be bad. Upcoming EV on
  the dashboard and in fixture briefs requires `is_fresh`; past EV shows the
  three states.

## 13j. Upcoming slips every 30 minutes -- and a slip is never "unchanged".

`ingest_slips` and a `summarise_upcoming` task (10 slips per run) moved into
the half-hourly signals DAG; the daily DAG keeps `summarise_played`.

The fetch used to MERGE all 200 slips every run, so it now skips a slip whose
payload hash and copy count match its newest stored row. **It almost never
skips**, and that is a fact about the data, not a bug: a slip payload carries
its legs' LIVE odds and probabilities. Of 40 slips compared 30 minutes apart,
40 had new probabilities, 39 new odds, 26 new copy counts and 10 in-play
scores. Runs logged `unchanged=1 merged=199` and `unchanged=0 merged=200`.
Because the hash covers the whole payload, each fetch appends ~200 rows --
about 9,600 a day of bronze at this cadence, against 6,585 in total before.

The same volatility broke the summariser. A verdict was rewritten whenever
`Slip.signature()` changed, and that hash includes leg odds, so every
summarised slip looked changed on every run: 28 of the first 30 half-hourly
verdicts were rewrites of slips already done that day, while 248 upcoming
slips never got one. A rewrite is now decided by `structure_signature()` --
legs, picks and form, no prices -- stored alongside, and never-summarised
slips are taken first. Dry run afterwards: 1,952 never summarised, 25 changed.

Open: whether bronze should keep a row per price drift. Hashing the payload
without leg odds and probabilities would cut the ~9,600 daily rows to real
edits and copy-count changes, at the cost of the price history of slips --
which `fact_odds_tick` already holds for the markets that matter.

## 13k. Links to the match: tested first, and 182 left out on purpose.

Every signal leg, EV row and slip leg links to its match on the bookmaker, from
the collector's `all_events.url` via the matcher's per-book event ids, loaded into
`CORE.dim_event_link` by `load_dims.py` (only new or changed links are written;
a rerun writes 0).

**Tested before publishing.** One upcoming fixture was opened on all five books:
each link landed on that match's own page with live 1X2 prices. msport sends a
first-time visitor to its welcome page once, then opens the match. The formats
sampled are the formats in use: every football URL in 60 days from msport,
sportybet and ilotbet carries the sr:match id; bet9ja and livescorebet use their
own event ids.

**The first load failed its own rerun**, with "Duplicate row detected": 450 of
95,021 fixture/book pairs held TWO events from the same book -- e.g. sportybet
sr:match:69133606 and 72852060 for one fixture. Those are different matches. A
link to the wrong one is worse than none, so where the URL carries an sr:match
id the one equal to the fixture's own wins (268 resolved), and otherwise the
pair has no link (182 dropped, bet9ja and livescorebet included, whose URLs
carry nothing to check against).

Coverage on the dashboard's joins: surebet legs 95%, EV rows 98%, slip legs
99%, upcoming slip legs 100%.

## 13l. "Where does the surebet stand now?" The tick store gave the wrong answer.

The surebet cards now say where every market that ever carried a surebet stands
now -- or stood at kick-off -- including below 1.0, with every detection marked
on a chart.

**The obvious rebuild was wrong 44% of the time.** Carry each book's last price
forward in `fact_odds_tick`, take the best per outcome, `1 / sum(1 / best)`.
Checked against the detector's own records, it reproduced 14 of 25 detections
within 0.005; the other 11 were off by up to +0.24 (rebuilt 1.30, detected
1.06). The price-age spread did not separate them: matched detections had
hour-old prices too, because a price standing unchanged never emits a tick. The
tick store records changes, so a price a book has WITHDRAWN and a price it has
simply left alone look identical. "Arbitrage now: 1.30" would have been this
project's signature failure, a plausible number with no error behind it.

**The fix replays the detector.** `odds/arbitrage_track.py` walks bronze in
fire_time order, keeps each book's latest payload with its fire_time (the live
pipeline's view), and runs the same `signals.arbitrage_rows` after every payload,
writing a point to `fact_arbitrage_track` only when the state changes. Checked:
**19 of 19 pre-kick-off detections equal the replay's state at their own market
time.** (A first check reported 1 of 29, from reading `.tail(1)` off an unordered
Snowflake result, which was the check's bug, not the replay's.) An incrementally
extended track matched a from-scratch replay point for point on every upcoming
fixture, once resume overlapped by 15 minutes: a payload stamped 20:32:52 reached
bronze after a 20:33 run had started, and a cursor without overlap would have
passed it forever.

**Both stores stay, with honest labels.** The card's "at kick-off" can read
"no price" while its table shows last odds of 1.30 and 2.05. Both are true: the
detector could no longer price the market across books, and the tick store still
holds each book's last change. The column is "Last recorded odds" for that
reason.

**Every historical chart is now ~10 points per line** (`dashboard/series.py`):
first and last kept, then the point whose absence misdraws the step line most,
greedily. Points under a marked opportunity are always kept, so a marker sits on
its line. Busy markets had run to 557 ticks.

`extract_ticks` and `track_arbitrage` moved to the 30-minute DAG: with a daily
extraction, 4 of the 5 surebet markets signalled since the last run had no price
history at all.

## 13m. msport suspends a market and leaves every outcome "active".

A surebet link to msport opened a page without the market. Bronze explained it
two ways.

**Suspension.** msport flags suspension on the MARKET (`status` 1) and leaves
each outcome's `isActive` at 1, and the parser only checked outcomes. Over 24
hours of bronze: 44,845 market snapshots at status 1, 89% in play, flipping
0 -> 1 -> 0 about 25,000 times a day -- the suspend/resume pattern around a goal
or a card -- and 4,896 pre-match. The parser now skips any market whose status
is not 0.

**Removal.** For Seravezza v Montevarchi, msport's 10:36 payload had no
Over/Under market at all, after a 10:04 surebet on it; by 12:14 it was back.
Nothing priced can show that, so `odds/live_state.py` checks every upcoming
signal leg against its book's LATEST payload every 30 minutes and records
whether it is offered and at what price (`fact_leg_availability`). Cards warn
when a leg is missing, and a viewer can flag a leg as not on the site
(`dashboard_leg_flag`), which hides it everywhere until restored.

## 13n. Settled results on slip legs, without re-deriving the mirror.

Every slip leg now carries how it resolved. `fact_team_market_result` mirrors
the verdict on the non-primary team's row (§13c), so the join must read the
PRIMARY team's row. Rather than restating the mirror rule in SQL,
`dim_market_outcome.primary_team` is computed by asking `team_perspective`
itself which team's row keeps a `won` intact. 13,603 of 22,832 legs resolved;
**all 4,740 1x2 legs checked against the final score were right**, away sides
included. Live match status comes from msport's payload, which bronze keeps
polling through full time (`fact_event_state`); a slip's own payload cannot
show it, because finished legs drop off the slip.

## 13o. A price that never changed was invisible on the EV chart.

An EV at livescorebet on Seravezza v Montevarchi showed no livescorebet line.
livescorebet had published that price once, at 06:59, and never changed it: the
tick store held one point, and one point drawn as a line draws nothing. The same
fact put the "latest odds" star at 06:59, before the EV detected against that
standing price at 10:04. Lines are now carried forward to now (or kick-off), as
a price holds until it changes, and the star sits at the end.

## 13p. Where the dashboard's time went, and what fixed it.

Measured before changing anything. A warm visit to market signals took 5.7s and
drew 38 charts and 45 tables; the slips page 10.4s first time. Server-side, with
every query cached, the slips page still spent 3.3s in Python: about 5,000
per-group pandas comparisons from two lambdas in one `groupby().agg()`. The worst
case was the COLD load after each pipeline write -- the cache is keyed on the
warehouse watermark, and a write every 30 minutes invalidated all of it: 10s for
slips and 18s for market signals, nearly all Snowflake.

Fixes, each measured:

* **Fetch what is shown.** Slip cards read one row per slip (`gold_slip_overview`)
  and fetch legs only for the 20 slips on screen, not all 23,000. Deep dives rank
  from `gold_fixture_popularity` instead of flattening raw slip payloads per visit.
* **Prepare once per write.** Signal merges and the backtest are cached against the
  watermark; only the clock-dependent upcoming/past split runs per visit.
* **One chart per tab.** A picker instead of a chart in every card: 38 -> 7.
* **Fragments.** A slider, toggle, tab or edited price reruns its own section.
* **Warm in the background.** A thread watches the watermark and re-runs every
  query used in the last six hours, plus registered computations, against the new
  mark before anyone asks. After a real write the market signals page loaded in
  2.3s; before, 15-18s.

What remains is Streamlit's model: every navigation re-executes the page and
re-sends every element. Bare-mode script time is now 0.02s (slips) and 0.11s
(signals) warm; the 2-5s a visitor sees is the round trip and rendering.

## 13q. Backtest of past positive EV, and why the sample is small.

Every past positive-EV opportunity (fixture, market, outcome) is replayed under
five sizings (flat 1%, 2% of bankroll, full, half and quarter Kelly) and three
timings (first seen, best EV, last before kick-off), staking from cash not tied
up in open bets. On the first run, 53 opportunities: half Kelly at the last
pre-kick-off price was best risk-adjusted (100 -> 328, worst drawdown 7%); full
Kelly ended highest (427) with double the drawdown and ran out of cash on 13-16
bets. One week of data; the page says so.

Only 53 of 236 past opportunities could be settled, and the reason was checked
rather than assumed: 114 are from 09-15/16 and await the daily match-stats
ingestion; 28 have no API-Football fixture; 36 are markets or lines the settlement
engine does not resolve; none were lost. No EV exists for 09-10..09-14 because
the machine was off.

## 13r. Form split by competition.

Briefs saw one pooled last-ten record per side. For Elche v Real Madrid that
record read Elche 2 wins in 10; split, it is 1W 3D 3L in La Liga plus three
friendlies, and Real Madrid's includes four friendlies. Form lines are now one per
competition, and all three prompts are told a cup or friendly record is weaker
evidence for a league match. League tables are the next step and need a league id
and country: `league_name` alone is ambiguous ("Premier League" spans 293 teams
in many countries), and the ingestor has no standings yet.

## 14. Where the build stands (snapshot, 2026-09-03).

Numbers move; the shape does not. Re-measure rather than trusting these.

| table | rows | written by |
|---|---:|---|
| `CORE.fact_team_match` | 298,241 | `spark/flatten.py` |
| `CORE.fact_team_market_result` | 5,963,080 | `spark/settle.py` |
| `CORE.dim_market_outcome` | 754 | `snowflake/load_dims.py` |
| `CORE.dim_fixture` | 4,747 | `snowflake/load_dims.py` |
| `CORE.dim_market` | 235 | `snowflake/load_dims.py` |
| `CORE.dim_bookmaker` | 5 | seeded in `ddl.sql` |
| `CORE.fact_arbitrage_signal` | 25 | `consumers/arb.py` |
| `CORE.fact_ev_signal` | 13 | `consumers/ev.py` |
| `CORE.bronze_slip_payload` | 400 | `slips/ingest.py` |
| `CORE.gold_slip_summary_ai` | 70 | `enrich/summarise.py` |
| `CORE.gold_fixture_summary_ai` | 5 | `enrich/fixture_summary.py` |
| `CORE.fact_odds_tick` | 32,057 | `odds/ticks.py` |
| `ANALYTICS.gold_slip_leg_history` | 6,685 | dbt |
| `ANALYTICS.gold_market_efficiency` | 7 | dbt |

71 pytest, 30 dbt tests, ruff and mypy clean over 49 files.
86% of slip legs carry settled history (was 49% before the rolling-window
fix in section 13a -- re-measure this, it is the health check that matters).

**Done:** producer, both consumers, warehouse schema and loaders, the Spark
flatten and settle jobs, the vendored settlement engine, the slip ingest, the
slip-to-history join, the LLM summariser, dbt (5 models + 2 singular tests),
GitHub Actions CI, the Airflow DAG, and the README.

**Remaining, in order:**

1. ~~**Rebuild the Airflow image.**~~ **DONE** -- and the green run above is
   the proof it was needed.
2. ~~**Boot the stack and trigger the DAG.**~~ **DONE 2026-09-03.** The
   scheduled run went green end to end: all ten tasks, 07:35:33 to 08:05:47,
   30 minutes. An earlier manual run failed at `flatten` and `ingest_slips`
   with four tasks `upstream_failed` -- almost certainly the pre-rebuild image,
   since the missing `httpx` is exactly what `ingest_slips` needs, but its logs
   were lost before they could confirm it.
3. ~~**Streamlit**: skeleton, tiles, the AI insights panel.~~ **Built and
   verified against live data.** Remaining: screenshots into
   `docs/screenshots/`, deploy to Community Cloud, verify the public URL from
   a fresh browser session.
4. **`dbt docs generate`** and a lineage screenshot into `docs/screenshots/`.
5. **Push and confirm CI's first green run.** It has never actually run.
6. Video, slides, `v1.0` tag.

**Known gaps, deliberate:** `msport`'s parser probably has sportybet's
suspended-market bug and has not been checked (section 1). Only 24,672 of
298,241 team-match rows carry xG, because API-Football populates statistics for
some leagues and not others. The slip corpus is two fetches.

---

## 15. Running things.

**Ports.** markets bronze 5440 · sources (event_matches, apifootball_events)
5433 · ingestor bronze 5434 · silver 5455 · redpanda **19092 from the host,
`redpanda:9092` from a container** (section 8e) · Airflow 8080.
Snowflake: database `ARBIBET_CAPSTONE`, schemas `CORE` (task-written) and
`ANALYTICS` (dbt-written, and a dbt full-refresh must never be able to drop
something a task paid an API call for).

**Local toolchain.** Python 3.12 venv at `.venv` (NOT 3.14 -- no pyspark
wheels). JDK 17 at `C:\jdk-17.0.20.1+1`. Airflow image
`arbibet-capstone-airflow:2.10.3`.

**Any Spark job** runs in the image, never on the host (section 6 says why):

```bash
MSYS_NO_PATHCONV=1 docker run --rm --env-file .env \
  -e "INGESTOR_DB_URL=postgresql://ingestor:PW@host.docker.internal:5434/ingestor" \
  -e "JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64" \
  -e "PYSPARK_PYTHON=/home/airflow/venv/bin/python" \
  -e "PYTHONPATH=/opt/project/src" \
  -v "C:/Users/Dumebi/Arbibet/arbibet-capstone:/opt/project" \
  -w /opt/project --entrypoint /bin/bash arbibet-capstone-airflow:2.10.3 \
  -c '$PIPELINE_PYTHON spark/flatten.py'
```

**Bounding knobs, all env-read, all defaulting to the full job:**
`FLATTEN_LIMIT` (payload count) · `FLATTEN_SINCE_DAYS` (incremental read;
the DAG uses 3) · `SETTLE_LIMIT` / `SETTLE_SINCE_DAYS` / `SETTLE_FAMILIES` /
`SETTLE_PERIODS` / `FORM_WINDOW` · `SUMMARISE_LIMIT` (default 20, costs money) ·
`ARB_RECORD_THRESHOLD` (0.98) · `EV_MIN` / `EV_MIN_PROBABILITY` ·
`TICKS_LIMIT` (fixtures to extract odds history for) ·
`IDLE_EXIT_SECONDS` (consumers exit after this quiet) ·
`WATCH_POLL_SECONDS` (20) / `WATCH_UNTIL_HOURS` (6) / `WATCH_REFRESH_SECONDS`
(300) for the freshness watcher.

Prove a long job on a bound before running it whole. Two eighty-minute Spark
runs were lost to failures a three-thousand-row run would have shown.

**A full local pass**, in dependency order:

```bash
python snowflake/apply_ddl.py && python snowflake/load_dims.py 7 7
docker compose up -d redpanda
docker compose exec redpanda rpk topic create market.ticks -p 1 -r 1
python producer/main.py 2 6
python consumers/arb.py ; python consumers/ev.py
python odds/ticks.py            # -> fact_odds_tick (after the consumers)
python slips/ingest.py
# flatten and settle in the container, per above
cd dbt && dbt run --profiles-dir . && dbt test --profiles-dir . && cd ..
python enrich/summarise.py
```

**Gates, never piped** (section 6): `ruff check . && mypy && pytest -q`.
