-- A login for the RUNNER's bot and publisher, in Supabase, instead of the
-- `postgres` superuser it uses today.
--
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run. Choose the
-- password yourself where marked, never commit it, then put the new URL in
-- .env as SUPABASE_DB_URL (same host, port and database; user `arbibet_runner`)
-- and restart the runner. Keep the postgres password for yourself only.
--
-- Why. Anyone or anything holding the runner's connection string can do exactly
-- what this role can. As `postgres` that is everything, including every other
-- project table and Supabase's own auth schema. As `arbibet_runner` it is the
-- three schemas the pipeline owns, and nothing else.

CREATE SCHEMA IF NOT EXISTS serving;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS bot;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'arbibet_runner') THEN
        CREATE ROLE arbibet_runner LOGIN PASSWORD '<choose a strong password>';
    END IF;
END
$$;

-- Own the schemas and everything already in them, so the runner can still
-- create and alter its tables (ensure_schema runs at every start).
ALTER SCHEMA serving OWNER TO arbibet_runner;
ALTER SCHEMA ops OWNER TO arbibet_runner;
ALTER SCHEMA bot OWNER TO arbibet_runner;
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('serving', 'ops', 'bot')
    LOOP
        EXECUTE format('ALTER TABLE %I.%I OWNER TO arbibet_runner', r.schemaname, r.tablename);
    END LOOP;
    FOR r IN
        SELECT sequence_schema, sequence_name FROM information_schema.sequences
        WHERE sequence_schema IN ('serving', 'ops', 'bot')
    LOOP
        EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO arbibet_runner', r.sequence_schema, r.sequence_name);
    END LOOP;
END
$$;

-- The dashboard keeps reading what the runner publishes, including tables the
-- runner creates from now on.
GRANT USAGE ON SCHEMA serving, ops TO dashboard_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA serving, ops TO dashboard_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE arbibet_runner IN SCHEMA serving, ops
    GRANT SELECT ON TABLES TO dashboard_reader;
GRANT INSERT, UPDATE ON serving.leg_flag TO dashboard_reader;

-- Subscribers' data stays out of every other role's reach. RLS is on with no
-- policies; the owner bypasses it, nobody else gets a row.
REVOKE ALL ON SCHEMA bot FROM PUBLIC, anon, authenticated, dashboard_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA bot FROM PUBLIC, anon, authenticated, dashboard_reader;
