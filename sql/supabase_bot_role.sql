-- A dedicated login for the RUNNER (bot + publisher) in Supabase, instead of
-- the `postgres` admin it used at first.
--
-- Part A (safe to run any time; scripts/apply_bot_role.py runs it over the
-- runner's existing connection): create the role WITHOUT a login, hand it the
-- three schemas the pipeline owns, and keep the dashboard's read access.
-- Nothing breaks while the runner still connects as postgres: postgres has
-- BYPASSRLS and stays a member of the new role.
--
-- Part B is yours alone, in the Supabase SQL editor -- the password never
-- goes through anything else:
--
--     ALTER ROLE arbibet_runner LOGIN PASSWORD '<choose a strong password>';
--
-- Then point SUPABASE_DB_URL in .env at the same host and database with user
-- `arbibet_runner.<project ref>` (the pooler wants role.projectref) and the
-- new password, and restart the runner. A leaked runner secret then exposes
-- these three schemas, not the project.

CREATE SCHEMA IF NOT EXISTS serving;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS bot;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'arbibet_runner') THEN
        CREATE ROLE arbibet_runner NOLOGIN;
    END IF;
END
$$;

-- postgres is not a superuser here; it must be a member of a role to hand it
-- ownership (and it keeps admin access through that membership).
GRANT arbibet_runner TO postgres;

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
        EXECUTE format(
            'ALTER SEQUENCE %I.%I OWNER TO arbibet_runner', r.sequence_schema, r.sequence_name
        );
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
-- policies; the owner (and BYPASSRLS admins) get rows, nobody else does.
REVOKE ALL ON SCHEMA bot FROM PUBLIC, anon, authenticated, dashboard_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA bot FROM PUBLIC, anon, authenticated, dashboard_reader;
