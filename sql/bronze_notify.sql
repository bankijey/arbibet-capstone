-- Tell the runner the moment a bookmaker payload lands in markets bronze.
--
-- Installed into arbibet-markets' database (scripts/install_bronze_notify.py).
-- It only SENDS: the payload is the fixture's event_id, 36 bytes. The runner's
-- listener (runner/listener.py) wakes on it, coalesces a burst of payloads
-- for one fixture into a single recompute, and falls back to polling every
-- 15 seconds if the connection drops.
--
-- The collector must never fail because of this trigger, so:
--   * any error inside is swallowed (EXCEPTION WHEN OTHERS);
--   * with no listener connected, Postgres discards notifications at once;
--   * Postgres also de-duplicates identical notifications within a
--     transaction, so a batch insert for one fixture sends one message.
--
-- Remove with:  DROP TRIGGER IF EXISTS bronze_payload_notify ON bronze_event_payloads;
--               DROP FUNCTION IF EXISTS arbibet_notify_bronze_payload();

CREATE OR REPLACE FUNCTION arbibet_notify_bronze_payload() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        PERFORM pg_notify('bronze_payload', NEW.event_id::text);
    EXCEPTION WHEN OTHERS THEN
        NULL;
    END;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS bronze_payload_notify ON bronze_event_payloads;
CREATE TRIGGER bronze_payload_notify
    AFTER INSERT ON bronze_event_payloads
    FOR EACH ROW EXECUTE FUNCTION arbibet_notify_bronze_payload();
