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
CREATE TABLE IF NOT EXISTS dim_market (
    market_base_id     NUMBER(38,0) NOT NULL,  -- betradar market id
    market_name        VARCHAR      NOT NULL,
    bet9ja_key         VARCHAR,                -- null => bet9ja does not price it
    livescorebet_type  NUMBER(38,0),           -- null => livescorebet does not price it
    is_arb_relevant    BOOLEAN      NOT NULL,  -- <=3 outcomes, so the engine can use it

    CONSTRAINT pk_dim_market PRIMARY KEY (market_base_id)
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
    leg_spread_seconds   NUMBER(38,0) NOT NULL,

    detected_at         TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_fact_arb PRIMARY KEY (signal_key),
    CONSTRAINT fk_arb_fixture FOREIGN KEY (event_id) REFERENCES dim_fixture (event_id),
    CONSTRAINT fk_arb_market  FOREIGN KEY (market_base_id) REFERENCES dim_market (market_base_id)
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

    payload_fire_time TIMESTAMP_TZ NOT NULL,
    detected_at      TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),

    CONSTRAINT pk_fact_ev PRIMARY KEY (signal_key),
    CONSTRAINT fk_ev_fixture FOREIGN KEY (event_id) REFERENCES dim_fixture (event_id),
    CONSTRAINT fk_ev_market  FOREIGN KEY (market_base_id) REFERENCES dim_market (market_base_id),
    CONSTRAINT fk_ev_book    FOREIGN KEY (bookmaker_id) REFERENCES dim_bookmaker (bookmaker_id)
);

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
