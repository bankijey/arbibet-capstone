-- A restricted login for the PUBLIC Streamlit dashboard, in Supabase.
--
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run. Choose the
-- password yourself where marked, and never commit it.
--
-- The runner connects as Supabase's own `postgres` user and creates the
-- tables. The dashboard gets this role instead, which can:
--   * read the published data (schemas `serving` and `ops`),
--   * write exactly one table: serving.leg_flag (viewer "not on site" flags).
-- Neither schema is exposed through Supabase's REST API, which only serves
-- `public` unless configured otherwise, so the anon key reaches none of it.

CREATE SCHEMA IF NOT EXISTS serving;
CREATE SCHEMA IF NOT EXISTS ops;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_reader') THEN
        CREATE ROLE dashboard_reader LOGIN PASSWORD '<choose a strong password>';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA serving, ops TO dashboard_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA serving, ops TO dashboard_reader;
-- Tables the runner creates later are covered too.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA serving, ops
    GRANT SELECT ON TABLES TO dashboard_reader;

-- The one write. The table is created here so the grant has something to name.
CREATE TABLE IF NOT EXISTS serving.leg_flag (
    event_id        text NOT NULL,
    market_id       text NOT NULL,
    bookmaker_name  text NOT NULL,
    active          boolean NOT NULL,
    flagged_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, market_id, bookmaker_name)
);
GRANT INSERT, UPDATE ON serving.leg_flag TO dashboard_reader;
