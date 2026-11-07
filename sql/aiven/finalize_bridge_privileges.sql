-- FCB-WP-009. Run as the administrative identity after bridge 0001-0008.
-- Existing bridge objects become migrator-owned. The runtime gets access only to
-- tables used by OAuth, browser leases and the two shared provider coordinators.
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fcb_migrator')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fcb_app')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fcb_signer') THEN
        RAISE EXCEPTION 'FCB_REQUIRED_ROLE_MISSING';
    END IF;
END $roles$;

DO $ownership$
DECLARE obj record;
BEGIN
    FOR obj IN
        SELECT c.relkind, c.oid::regclass AS qualified_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
    LOOP
        IF obj.relkind = 'v' THEN
            EXECUTE format('ALTER VIEW %s OWNER TO fcb_migrator', obj.qualified_name);
        ELSIF obj.relkind = 'm' THEN
            EXECUTE format('ALTER MATERIALIZED VIEW %s OWNER TO fcb_migrator', obj.qualified_name);
        ELSE
            EXECUTE format('ALTER TABLE %s OWNER TO fcb_migrator', obj.qualified_name);
        END IF;
    END LOOP;

    -- PostgreSQL rejects ALTER SEQUENCE OWNER for SERIAL/IDENTITY sequences
    -- linked to a table. ALTER TABLE above already transfers those sequences.
    -- Transfer only independent sequences here.
    FOR obj IN
        SELECT c.oid::regclass AS qualified_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'S'
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid
                AND d.deptype IN ('a', 'i')
          )
    LOOP
        EXECUTE format('ALTER SEQUENCE %s OWNER TO fcb_migrator', obj.qualified_name);
    END LOOP;

    FOR obj IN
        SELECT p.oid::regprocedure AS qualified_name
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
    LOOP
        EXECUTE format('ALTER ROUTINE %s OWNER TO fcb_migrator', obj.qualified_name);
    END LOOP;
END $ownership$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, fcb_app, fcb_signer;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, fcb_app, fcb_signer;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, fcb_app, fcb_signer;

GRANT SELECT ON TABLE schema_migrations TO fcb_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    oauth_authorization_codes,
    oauth_access_tokens,
    oauth_refresh_tokens,
    oauth_claude_dcr_clients,
    browser_session_leases,
    api_football_quota_counters,
    api_football_quota_leases,
    api_football_quota_attempts,
    sxbet_rate_attempts,
    sxbet_rate_cooldowns
TO fcb_app;

-- The signer remains isolated from bridge objects. No signer migration or signer
-- privilege is introduced by this script.
