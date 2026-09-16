-- Arbibet capstone — Snowflake schema.
--
-- Two schemas, two owners:
--   CORE      written by the pipeline (Kafka consumers, PySpark dim refresh)
--   ANALYTICS written by dbt (stg_* and gold_*) plus the LLM summariser
--
-- Snowflake does not enforce PRIMARY KEY or UNIQUE (only NOT NULL). The
-- constraints below are documentation for readers and for dbt; the actual
-- enforcement is the `unique` / `not_null` / `relationships` tests in
-- dbt/models/schema.yml. Stating that here so nobody later mistakes a
-- declared key for a guaranteed one.

CREATE DATABASE IF NOT EXISTS ARBIBET_CAPSTONE;
USE DATABASE ARBIBET_CAPSTONE;

CREATE SCHEMA IF NOT EXISTS CORE;
CREATE SCHEMA IF NOT EXISTS ANALYTICS;

USE SCHEMA CORE;

-- ---------------------------------------------------------------- dimensions

-- One row per real-world fixture, sourced from arbibet-matcher's event_matches.
CREATE TABLE IF NOT EXISTS dim_fixture (
    event_id        VARCHAR(36)  NOT NULL,   -- matcher UUID; the canonical key
    apifootball_id  NUMBER(38,0),            -- null until the matcher resolves one
    kickoff_at      TIMESTAMP_TZ NOT NULL,
    tournament      VARCHAR,
    home_team       VARCHAR,
    away_team       VARCHAR,
    -- API-Football team ids, carried so a fixture reaches its sides' history
    -- without a second trip to the sources DB: they are what fact_team_match
    -- and fact_team_market_result are keyed on. Null for the ~17% of fixtures
    -- the matcher has not resolved an apifootball leg for -- those are still
    -- priceable, they simply have no history behind them.
    home_team_id    NUMBER(38,0),
    away_team_id    NUMBER(38,0),
    -- The betradar match id ("sr:match:73936890"). Every book's e_id for a
    -- fixture carries the same one, so it identifies the fixture rather than
    -- a book's view of it -- and it is the ONLY key a booking slip joins on:
    -- slips name fixtures by sr:match and teams by sr:competitor, neither of
    -- which appears anywhere else here.
    sr_match_id     VARCHAR(64),
    loaded_at       TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_dim_fixture PRIMARY KEY (event_id)
);

-- Five rows. Small enough to read, and the flags explain pipeline behaviour
-- that is otherwise invisible in the data.
CREATE OR REPLACE TABLE dim_bookmaker (
    bookmaker_id          INT         NOT NULL,  -- surrogate key, assigned in the seed below
    bookmaker_name        VARCHAR     NOT NULL,
    is_betradar_native    BOOLEAN     NOT NULL,  -- false => needs the crosswalk
    publishes_probability BOOLEAN     NOT NULL,  -- false => EV borrows p from a book that does

    CONSTRAINT pk_dim_bookmaker PRIMARY KEY (bookmaker_id)
);

-- The market crosswalk, loaded from msports_matches.csv.
-- Grain is the BASE betradar market id, without a specifier: id 18 is
-- "Over/Under" once, and the 2.5 lives on the fact, not here.
-- A fixture's page on each bookmaker, so a signal or a slip leg can link to
-- the match it names. From the collector's `all_events.url`, via the matcher's
-- per-book event ids. Tested on all five books before being published: every
-- sampled link opened that match's own page.
CREATE TABLE IF NOT EXISTS dim_event_link (
    event_id      VARCHAR(36)  NOT NULL,
    bookmaker_id  NUMBER(38,0) NOT NULL,
    url           VARCHAR      NOT NULL,
    loaded_at     TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_dim_event_link PRIMARY KEY (event_id, bookmaker_id)
);

CREATE TABLE IF NOT EXISTS dim_market (
    market_base_id     NUMBER(38,0) NOT NULL,  -- betradar market id
    market_name        VARCHAR      NOT NULL,
    bet9ja_key         VARCHAR,                -- null => bet9ja does not price it
    livescorebet_type  NUMBER(38,0),           -- null => livescorebet does not price it
    is_arb_relevant    BOOLEAN      NOT NULL,  -- <=3 outcomes, so the engine can use it

    CONSTRAINT pk_dim_market PRIMARY KEY (market_base_id)
);

-- The betradar market/outcome taxonomy, materialised from the settlement
-- engine's two maps. They are Python data, so without this only Python can
-- use them -- and a booking slip's `(market.id, outcome.id)` needs resolving
-- in SQL, as does anything dbt or the dashboard asks later.
--
-- Loaded by `dims.market_outcome_rows()`, which READS the maps rather than
-- restating them, so there is still one definition.
CREATE TABLE IF NOT EXISTS dim_market_outcome (
    market_id       VARCHAR(32)  NOT NULL,   -- betradar market id, as text
    outcome_id      VARCHAR(32)  NOT NULL,   -- betradar outcome id, as text
    market_family   VARCHAR      NOT NULL,   -- 1x2, total_goals, btts, ...
    period          VARCHAR      NOT NULL,   -- match | 1h | 2h
    time_basis      VARCHAR,                 -- regular | full | 1h | 2h | other
    side            VARCHAR      NOT NULL,   -- home, over, yes, ...
    has_line        BOOLEAN      NOT NULL,   -- does side_or_line need an "@line"
    -- symmetric | directional | team_scoped_home | team_scoped_away |
    -- not_team_relevant. Decides whether two teams' histories may be pooled:
    -- both sides observe "over 2.5" identically, but "home wins" is a
    -- different claim for each of them.
    classification  VARCHAR,
    market_name     VARCHAR,
    -- home | away | both: whose row in fact_team_market_result carries this
    -- side's FIXTURE-level verdict. The other team's row is mirrored.
    primary_team    VARCHAR,

    CONSTRAINT pk_dim_market_outcome PRIMARY KEY (market_id, outcome_id)
);

-- ---------------------------------------------------------------- facts

-- One row per detected arbitrage: (fixture, market, snapshot).
-- Legs stay nested: they are only ever read as a set, and flattening them
-- would repeat the arbitrage value on every row for no gain.
CREATE TABLE IF NOT EXISTS fact_arbitrage_signal (
    signal_key          VARCHAR(64)  NOT NULL,  -- sha256(event_id, market_id, legs) — idempotency
    event_id            VARCHAR(36)  NOT NULL,
    market_id           VARCHAR(64)  NOT NULL,  -- full canonical id incl. specifier, e.g. '18;2.5'
    market_base_id      NUMBER(38,0) NOT NULL,  -- joins dim_market
    specifier           VARCHAR(32),            -- '2.5', '0:1', null when the market has none

    arbitrage           FLOAT        NOT NULL,  -- 1/sum(1/odds); > 1 is a surebet
    n_legs              NUMBER(38,0) NOT NULL,
    legs                VARIANT      NOT NULL,  -- [{outcome_id, bookmaker, odds}, ...]

    -- Leg freshness. Bronze is append-only-on-change, so the latest payload
    -- per book can be minutes apart. An arbitrage computed across stale legs
    -- is not tradeable, and the spread below is how the dashboard says so.
    oldest_leg_fire_time TIMESTAMP_TZ NOT NULL,
    newest_leg_fire_time TIMESTAMP_TZ NOT NULL,
    leg_spread_seconds   NUMBER(38,0) NOT NULL,   -- refused above 300 at detection

    -- MARKET time: the newest leg's fire_time, the moment the whole price set
    -- was last observable. It used to default to consumer wall-clock, which
    -- made a pre-kick-off snapshot consumed late look like an in-play signal.
    detected_at         TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    -- OUR time: when a consumer last processed it.
    consumed_at         TIMESTAMP_TZ,

    CONSTRAINT pk_fact_arb PRIMARY KEY (signal_key),
    CONSTRAINT fk_arb_fixture FOREIGN KEY (event_id) REFERENCES dim_fixture (event_id),
    CONSTRAINT fk_arb_market  FOREIGN KEY (market_base_id) REFERENCES dim_market (market_base_id)
);

-- The arbitrage a market offered over time, for every market that has ever
-- carried a fresh surebet. Rebuilt by replaying bronze through the detector
-- itself (`odds/arbitrage_track.py`), NOT from the tick store: a tick rebuild
-- reproduced only 14 of 25 detections, overstating by up to +0.24, because the
-- tick store cannot see a price being withdrawn. One row per STATE CHANGE,
-- below 1.0 included. `arbitrage` NULL means no cross-book price at that moment.
CREATE TABLE IF NOT EXISTS fact_arbitrage_track (
    event_id              VARCHAR(36)  NOT NULL,
    market_id             VARCHAR(64)  NOT NULL,   -- full id, e.g. '18;2.5'
    observed_at           TIMESTAMP_TZ NOT NULL,   -- the payload that changed the picture
    arbitrage             FLOAT,
    leg_spread_seconds    NUMBER(38,0),
    newest_leg_fire_time  TIMESTAMP_TZ,
    loaded_at             TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_fact_arb_track PRIMARY KEY (event_id, market_id, observed_at)
);

-- Match status as the books show it now, for fixtures on recent booking slips.
-- From msport's payload (eventMatchStatus / scoreOfWholeMatch / playedTime),
-- written by odds/live_state.py only when it changes.
CREATE TABLE IF NOT EXISTS fact_event_state (
    event_id      VARCHAR(36)  NOT NULL,
    match_status  VARCHAR,          -- 'Not start', 'H1', 'HT', 'H2', 'Ended', ...
    score         VARCHAR,          -- '1:0'
    played_time   VARCHAR,          -- "44'55\""
    source_book   VARCHAR(32),
    fired_at      TIMESTAMP_TZ,     -- the payload this was read from
    checked_at    TIMESTAMP_TZ,

    CONSTRAINT pk_fact_event_state PRIMARY KEY (event_id)
);

-- Whether each leg of an upcoming signal is still offered, and at what price,
-- in the book's LATEST payload -- parsed by the detector's own crosswalk.
CREATE TABLE IF NOT EXISTS fact_leg_availability (
    event_id       VARCHAR(36)  NOT NULL,
    market_id      VARCHAR(64)  NOT NULL,
    outcome_id     VARCHAR(32)  NOT NULL,
    bookmaker_id   NUMBER(38,0) NOT NULL,
    offered        BOOLEAN      NOT NULL,
    current_odds   FLOAT,
    book_fired_at  TIMESTAMP_TZ,
    checked_at     TIMESTAMP_TZ,

    CONSTRAINT pk_fact_leg_availability PRIMARY KEY (event_id, market_id, outcome_id, bookmaker_id)
);

-- Legs a viewer flagged from the dashboard as not on the site. Hidden from the
-- signal tables for as long as `active`; unflagging sets it false, never
-- deletes, so the record of who-saw-what stays.
CREATE TABLE IF NOT EXISTS dashboard_leg_flag (
    event_id        VARCHAR(36)  NOT NULL,
    market_id       VARCHAR(64)  NOT NULL,
    bookmaker_name  VARCHAR(32)  NOT NULL,
    active          BOOLEAN      NOT NULL,
    flagged_at      TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_dashboard_leg_flag PRIMARY KEY (event_id, market_id, bookmaker_name)
);

-- One row per positive-EV outcome. Grain is the OUTCOME, not the market:
-- EV is an per-outcome claim, unlike arbitrage which is a property of the set.
CREATE TABLE IF NOT EXISTS fact_ev_signal (
    signal_key       VARCHAR(64)  NOT NULL,
    event_id         VARCHAR(36)  NOT NULL,
    market_id        VARCHAR(64)  NOT NULL,
    market_base_id   NUMBER(38,0) NOT NULL,
    specifier        VARCHAR(32),
    outcome_id       VARCHAR(32)  NOT NULL,   -- canonical betradar outcome id
    outcome_name     VARCHAR,

    bookmaker_id     INT          NOT NULL,   -- the book offering these odds
    odds             FLOAT        NOT NULL,
    implied_p        FLOAT        NOT NULL,   -- the p used in the EV calculation
    p_source         VARCHAR(32)  NOT NULL,   -- which book supplied that p; provenance matters
    ev               FLOAT        NOT NULL,   -- implied_p * odds - 1

    payload_fire_time TIMESTAMP_TZ NOT NULL,          -- when the PRICE was published
    -- When the PROBABILITY was published: the most recent of sportybet/msport.
    -- NULL on rows written before 2026-09-15, which never recorded it.
    probability_fire_time      TIMESTAMP_TZ,
    probability_spread_seconds NUMBER(38,0),         -- |price - probability|; refused above 300
    detected_at      TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),  -- later of the two
    consumed_at      TIMESTAMP_TZ,                                     -- when a consumer processed it

    CONSTRAINT pk_fact_ev PRIMARY KEY (signal_key),
    CONSTRAINT fk_ev_fixture FOREIGN KEY (event_id) REFERENCES dim_fixture (event_id),
    CONSTRAINT fk_ev_market  FOREIGN KEY (market_base_id) REFERENCES dim_market (market_base_id),
    CONSTRAINT fk_ev_book    FOREIGN KEY (bookmaker_id) REFERENCES dim_bookmaker (bookmaker_id)
);

-- Team-match facts, flattened from api-football-ingestor's
-- `bronze_fixture_details` by the Spark job. One row per (fixture, team) --
-- arbibet-silver's D7 grain, reused because it is the right one.
--
-- Column names mirror silver's `fact_team_match` deliberately. Two of them
-- encode traps silver measured and D16 records, and renaming them would throw
-- the knowledge away:
--   * `goals_prevented_fixture` -- API-Football reports goals_prevented
--     IDENTICALLY for both teams in a fixture, so it is a fixture-level number
--     wearing a per-team costume. The per-team keeper signal is the derived
--     `goals_against_minus_xg_against` beside it.
--   * `passes_accurate` is a COUNT, not a percentage. `passes_pct` is the
--     percentage. The payload publishes both under confusable names.
--
-- The period-resolved score columns are not decoration: `settlement`'s
-- `_select_score(scores, basis)` picks a container per market (D19).
--
-- READ THIS BEFORE USING ANY SCORE COLUMN. Three names look interchangeable
-- and are not:
--
--   score_fulltime_*  API-Football's raw field. The score at 90 minutes.
--   goals_*_reg       h1 + h2. The SAME number, named for what settlement
--                     calls it. **This is the default basis for almost every
--                     market** -- 1X2, over/under, BTTS, handicaps all settle
--                     on 90 minutes, and extra time does not count. Silver's
--                     D26(a): absence of a period signal resolves to
--                     `regular`, covering 142 market ids in its cohort.
--   goals_*_full      reg + et. A DIFFERENT number. Selected only by markets
--                     whose time_basis explicitly declares `full` -- the
--                     "to qualify" family, where extra time does count.
--
-- Penalties never enter either basis. Silver measured an AET final where 3 of
-- 6 markets settled differently on reg versus full, which is why both are
-- stored and why the market picks, never the loader.
--
-- No foreign keys: these are API-Football ids for HISTORICAL fixtures, while
-- `dim_fixture` holds the upcoming ones this pipeline prices. Different
-- populations, deliberately unjoined. The link that matters is
-- `team_id` <-> `apifootball_events.home_id`/`away_id`, which is a lookup.
CREATE TABLE IF NOT EXISTS fact_team_match (
    fixture_id      NUMBER(38,0) NOT NULL,   -- API-Football fixture id
    team_id         NUMBER(38,0) NOT NULL,   -- API-Football team id
    team_name       VARCHAR      NOT NULL,
    opponent_id     NUMBER(38,0) NOT NULL,
    opponent_name   VARCHAR      NOT NULL,
    is_home         BOOLEAN      NOT NULL,

    match_date      TIMESTAMP_TZ NOT NULL,
    season          NUMBER(38,0),
    league_name     VARCHAR,
    round           VARCHAR,
    status          VARCHAR(8),              -- FT | AET | PEN

    -- raw score containers, exactly the four the payload publishes (D30)
    score_halftime_for      NUMBER(38,0),
    score_halftime_against  NUMBER(38,0),
    score_fulltime_for      NUMBER(38,0),
    score_fulltime_against  NUMBER(38,0),
    score_extratime_for     NUMBER(38,0),
    score_extratime_against NUMBER(38,0),
    score_penalty_for       NUMBER(38,0),
    score_penalty_against   NUMBER(38,0),

    -- D18 period components, derived at load time; the settlement basis
    goals_for_h1        NUMBER(38,0),
    goals_for_h2        NUMBER(38,0),
    goals_for_reg       NUMBER(38,0),
    goals_for_et        NUMBER(38,0),
    goals_for_pens      NUMBER(38,0),
    goals_for_full      NUMBER(38,0),
    goals_against_h1    NUMBER(38,0),
    goals_against_h2    NUMBER(38,0),
    goals_against_reg   NUMBER(38,0),
    goals_against_et    NUMBER(38,0),
    goals_against_pens  NUMBER(38,0),
    goals_against_full  NUMBER(38,0),

    goals_for       NUMBER(38,0) NOT NULL,
    goals_against   NUMBER(38,0) NOT NULL,
    result          VARCHAR(1)   NOT NULL,   -- W | D | L, on the fulltime basis

    -- match statistics: how the team played, not just what it scored
    xg                              FLOAT,
    xg_against                      FLOAT,
    goals_prevented_fixture         FLOAT,   -- fixture-level; see note above
    goals_against_minus_xg_against  FLOAT,   -- the per-team keeper signal (D16)
    possession      NUMBER(5,2),
    total_shots     NUMBER(38,0),
    shots_on        NUMBER(38,0),
    shots_off       NUMBER(38,0),
    blocked_shots   NUMBER(38,0),
    shots_inside    NUMBER(38,0),
    shots_outside   NUMBER(38,0),
    fouls           NUMBER(38,0),
    corners         NUMBER(38,0),
    offsides        NUMBER(38,0),
    yellow          NUMBER(38,0),
    red             NUMBER(38,0),
    saves           NUMBER(38,0),
    passes_total    NUMBER(38,0),
    passes_accurate NUMBER(38,0),            -- a COUNT (D16)
    passes_pct      NUMBER(5,2),

    loaded_at       TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_fact_team_match PRIMARY KEY (fixture_id, team_id)
);

-- Settled markets, per team, per historical fixture -- silver's D15 grain.
-- Produced by the vendored settlement engine: `settlement.settle()` returns a
-- fixture-level verdict, `team_perspective` re-expresses it from one team's
-- point of view (mirroring the verdict for directional families, leaving it
-- unchanged for symmetric ones).
--
-- D15's payoff, and the reason this table earns its place: every settled
-- market becomes a form dimension for free. "Over 2.5 in 7 of the last 10",
-- "BTTS rate at home", "clean-sheet run" -- all filters on this one table, no
-- schema change. Head-to-head is the same query plus `opponent_id = ?`.
--
-- Identity is `(market_family, period)` and NOT the betradar market id:
-- ids 18 / 68 / 90 are one family at three periods (silver D5), and several
-- ids may legitimately land on one family.
CREATE TABLE IF NOT EXISTS fact_team_market_result (
    fixture_id      NUMBER(38,0) NOT NULL,
    team_id         NUMBER(38,0) NOT NULL,
    opponent_id     NUMBER(38,0) NOT NULL,
    match_date      TIMESTAMP_TZ NOT NULL,
    is_home         BOOLEAN      NOT NULL,

    market_family   VARCHAR      NOT NULL,   -- 1x2, total_goals, btts, ...
    period          VARCHAR      NOT NULL,   -- match | 1h | 2h
    side_or_line    VARCHAR      NOT NULL,   -- canonical side, "@" line where the family declares one

    verdict         VARCHAR(16)  NOT NULL,   -- won | lost | push | void | unsettleable
    -- Populated ONLY when verdict = 'unsettleable'; a settled verdict never
    -- carries one, because the reason IS the verdict. The engine enforces this
    -- invariant, so a row breaking it means the loader lied, not the engine.
    reason          VARCHAR,

    loaded_at       TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_fact_tmr PRIMARY KEY (fixture_id, team_id, market_family, period, side_or_line)
);

-- Booking slips collected from msport, stored VERBATIM.
--
-- Raw, not parsed, and that is the point. Slips have no history endpoint: a
-- leg vanishes from `bettableBetSlip` the moment its match kicks off, and
-- msport will not give it back. This session found three parser defects in one
-- day -- collapsed over/under lines, un-normalised handicaps, suspended
-- markets read as live -- and every one was fixable only because the raw bytes
-- were still there to re-parse. Without this table, a slip-parser bug is
-- permanent data loss.
--
-- Everything downstream -- leg composition, soundness, popularity, status --
-- is a dbt model over this. The Python job only fetches, hashes and merges.
--
-- Write-on-change, keyed on the content hash (the same rule as
-- arbibet-markets D5). The hash covers the WHOLE payload including
-- `followedTimes`, so a slip gaining followers writes a new row: popularity
-- over time is one of the questions this table exists to answer, and at ~200
-- slips an hour the volume is nothing.
CREATE TABLE IF NOT EXISTS bronze_slip_payload (
    source            VARCHAR(32)  NOT NULL,   -- 'msport'; a sportybet booking code would collide otherwise
    share_code        VARCHAR(32)  NOT NULL,   -- the string punters actually copy
    payload_hash      VARCHAR(64)  NOT NULL,   -- sha256 of the canonical payload

    payload           VARIANT      NOT NULL,   -- the detail response, verbatim

    -- Promoted from the list endpoint so the common queries need no FLATTEN.
    list_id           VARCHAR(64),             -- also the pagination cursor; the only way to resume a fetch
    followed_times    NUMBER(38,0),
    folds             NUMBER(38,0),

    -- first_ is omitted by the writer so it defaults on insert and is never
    -- updated; last_ is written every time, so the pair brackets the window in
    -- which this exact content was live.
    first_fetched_at  TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    last_fetched_at   TIMESTAMP_TZ NOT NULL,

    CONSTRAINT pk_bronze_slip PRIMARY KEY (source, share_code, payload_hash)
);

-- LLM-written verdicts on booking slips, one row per slip per leg-set.
--
-- In CORE rather than ANALYTICS despite being a gold-layer artifact, because
-- CORE is where task-written tables live and ANALYTICS is dbt's. A dbt
-- full-refresh must never be able to drop something a task wrote and paid an
-- API call for.
--
-- Keyed on `leg_signature` as well as `share_code`: a slip loses legs as its
-- matches kick off, so the same code describes a different bet over time. A
-- new leg-set earns a new summary; re-running over an unchanged one costs
-- nothing.
CREATE TABLE IF NOT EXISTS gold_slip_summary_ai (
    share_code      VARCHAR(32)  NOT NULL,
    leg_signature   VARCHAR(64)  NOT NULL,   -- sha256 over the legs summarised, prices included
    -- The same WITHOUT prices: legs, picks and form. This decides a rewrite;
    -- leg odds move on nearly every fetch and must not. NULL before 2026-09-15.
    structure_signature VARCHAR(64),

    summary         VARCHAR      NOT NULL,   -- the model's text
    model           VARCHAR(64)  NOT NULL,   -- which model wrote it
    legs            NUMBER(38,0) NOT NULL,
    legs_with_history NUMBER(38,0) NOT NULL, -- how much evidence it was given
    followed_times  NUMBER(38,0),

    generated_at    TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_gold_slip_summary PRIMARY KEY (share_code, leg_signature)
);

-- One row per PRICE CHANGE: (fixture, market, outcome, book, moment).
--
-- The signal tables say an opportunity existed at an instant. This says how
-- the prices got there and what they did next, which is the difference
-- between asserting a surebet and showing one.
--
-- No new collection was needed. arbibet-markets bronze is append-only and
-- writes a row whenever a book's response changes, so it is already a tick
-- store; `odds/ticks.py` replays it through the same crosswalk the producer
-- uses. That matters for trust: the chart and the signal are parsed by one
-- code path, so a chart that disagrees with a signal is a real disagreement
-- rather than two implementations drifting.
--
-- GRAIN IS A PRICE CHANGE, NOT A PAYLOAD. Bronze writes when ANY part of a
-- book's response moves, so consecutive payloads routinely carry an identical
-- price for a given outcome. Those are collapsed on the way in: a row here
-- means this outcome's price at this book became this number at this moment,
-- which is exactly what a step chart needs and is a fraction of the volume.
--
-- Scope is deliberately bounded to markets that appear in a signal. Every
-- market in every payload would be a few hundred thousand rows, almost all of
-- them never looked at.
CREATE TABLE IF NOT EXISTS fact_odds_tick (
    event_id      VARCHAR(36)  NOT NULL,
    market_id     VARCHAR(64)  NOT NULL,  -- canonical, incl. specifier: '18;2.5'
    outcome_id    VARCHAR(32)  NOT NULL,
    -- What the BOOK called this outcome. The only honest label for markets the
    -- settlement taxonomy does not cover: it is built from the engine's maps,
    -- and the engine settles from scores, so corners markets arrive with ids
    -- and no names. Betradar outcome ids are MARKET-SCOPED -- id 12 is `over`
    -- in one market and `1-2` in another -- so the id cannot be resolved on
    -- its own, and guessing produced "outcome 12" on the flagship table.
    outcome_name  VARCHAR,
    bookmaker_id  INT          NOT NULL,
    odds          FLOAT        NOT NULL,
    -- When the book fired this price, NOT when we read it. Line movement is a
    -- claim about the market's clock, and write_time would smear it with our
    -- own polling cadence.
    fire_time     TIMESTAMP_TZ NOT NULL,
    loaded_at     TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_fact_odds_tick
        PRIMARY KEY (event_id, market_id, outcome_id, bookmaker_id, fire_time),
    CONSTRAINT fk_tick_fixture FOREIGN KEY (event_id) REFERENCES dim_fixture (event_id),
    CONSTRAINT fk_tick_book    FOREIGN KEY (bookmaker_id) REFERENCES dim_bookmaker (bookmaker_id)
);

-- LLM-written prose briefs on a single fixture, for the deep-dive pages.
--
-- Keyed on `evidence_signature` as well as `event_id`, for the reason
-- gold_slip_summary_ai is: a brief describes numbers that move independently
-- of the fixture. Prices tick, history settles, team ids resolve or stop
-- resolving. Hashing the fixture id alone would let a brief outlive every
-- number in it -- which is exactly how a slip summary came to sit on the
-- dashboard citing form of 90% beside a table reading "no history".
--
-- In CORE rather than ANALYTICS, like the slip summaries: CORE is where
-- task-written tables live, and a dbt full-refresh must never be able to drop
-- something a task paid an API call for.
CREATE TABLE IF NOT EXISTS gold_fixture_summary_ai (
    event_id            VARCHAR(36)  NOT NULL,
    evidence_signature  VARCHAR(64)  NOT NULL,

    summary             VARCHAR      NOT NULL,
    model               VARCHAR      NOT NULL,

    -- What the model was actually shown, so a brief can be audited without
    -- re-deriving the inputs it was written from.
    markets_described   NUMBER(38,0),
    sides_with_history  NUMBER(38,0),

    -- The numbers this brief was written against, so the NEXT run can ask how
    -- far they have moved rather than only whether they moved at all.
    --
    -- `evidence_signature` alone cannot answer that: it is a hash, and a hash
    -- of a rounded value still changes whenever the value crosses a rounding
    -- boundary. Measured on our own 156,171 repricings, banding implied
    -- probability to 0.02 leaves 46.7% of moves in the same band -- so a hash
    -- of bands still rewrote a brief on over half of all price ticks, most of
    -- them movements of well under one point of probability. Keeping the
    -- values makes the comparison a real threshold on CHANGE.
    evidence            VARIANT,

    generated_at        TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_gold_fixture_summary PRIMARY KEY (event_id, evidence_signature)
);

-- LLM verdicts on matches that have FINISHED. Written once, never revised.
--
-- The other two AI tables are keyed on a signature of their evidence, because
-- a slip's form and a fixture's prices move and a summary describing them must
-- move too. This one is keyed on `event_id` alone, deliberately: the match is
-- over, the score is a settled fact, and a description of a finished thing has
-- nothing to chase. Re-running this job skips anything already written.
--
-- The trade-off, stated because it is real: if the settlement engine later
-- corrects a verdict, the summary here will not reflect it. Deleting the row
-- is how you ask for a rewrite.
CREATE TABLE IF NOT EXISTS gold_fixture_result_ai (
    event_id       VARCHAR(36)  NOT NULL,

    summary        VARCHAR      NOT NULL,
    model          VARCHAR      NOT NULL,

    -- What the model was shown, so a verdict can be audited without
    -- re-deriving its inputs.
    markets_shown  NUMBER(38,0),
    signals_shown  NUMBER(38,0),

    generated_at   TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_gold_fixture_result PRIMARY KEY (event_id)
);

-- How far a job has already processed a given key, so the next run can skip
-- what has not changed.
--
-- `scope` namespaces the keys: the producer's position for a fixture is "the
-- newest bronze write I have published", which is a different fact from the
-- tick extractor's "the newest price I have stored". One table, no collisions.
--
-- Deliberately tiny and deliberately non-authoritative. Losing this table
-- costs one slow run, never a wrong one -- every reader treats a missing
-- position as "process everything".
CREATE TABLE IF NOT EXISTS pipeline_cursor (
    scope      VARCHAR(32)  NOT NULL,   -- 'producer', 'ticks', ...
    key        VARCHAR(64)  NOT NULL,   -- usually an event_id
    position   TIMESTAMP_TZ NOT NULL,   -- the newest input already handled
    updated_at TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_pipeline_cursor PRIMARY KEY (scope, key)
);

-- ---------------------------------------------------- schema evolution
--
-- `CREATE TABLE IF NOT EXISTS` is CREATE-only. Adding a column above and
-- re-running this file does nothing to a table that already exists -- no
-- error, no change, and the omission only surfaces when a query fails on the
-- missing identifier. Every column added after a table first shipped needs an
-- explicit statement here.
--
-- `IF NOT EXISTS` on the ALTER keeps the file idempotent, which is the whole
-- reason it can be re-run.

ALTER TABLE dim_fixture ADD COLUMN IF NOT EXISTS sr_match_id VARCHAR(64);
ALTER TABLE dim_market_outcome ADD COLUMN IF NOT EXISTS classification VARCHAR;
ALTER TABLE fact_odds_tick ADD COLUMN IF NOT EXISTS outcome_name VARCHAR;

-- ---------------------------------------------------------------- seed

-- Ids are literal, not generated: they must survive a CREATE OR REPLACE, and
-- AUTOINCREMENT restarts its sequence on one. Names are BRONZE's spelling --
-- 'msport' and 'ilotbet' -- because bronze sits downstream of arbibet-markets'
-- alias map and is therefore the platform's canonical vocabulary. The matcher's
-- 'msports'/'ilobet' never reach this database.
INSERT INTO dim_bookmaker
    (bookmaker_id, bookmaker_name, is_betradar_native, publishes_probability)
VALUES
    (1, 'sportybet',    TRUE,  TRUE),
    (2, 'msport',       TRUE,  TRUE),
    (3, 'ilotbet',      TRUE,  FALSE),
    (4, 'bet9ja',       FALSE, FALSE),
    (5, 'livescorebet', FALSE, FALSE);
