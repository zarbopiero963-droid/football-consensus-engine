-- FCB-WP-009. Run as the administrative identity after bridge 0001-0008.
-- Existing bridge objects become migrator-owned. The runtime gets access only to
-- tables used by OAuth, browser leases and the two shared provider coordinators.
-- A non-superuser administrator (Aiven's avnadmin) must be able to SET ROLE fcb_migrator to
-- hand objects over; sql/aiven/upsert_bridge_roles.sql grants exactly that in the same
-- transaction. Catalog relations, functions and operators are schema-qualified (audit F3).
--
-- Runtime allowlist (unchanged by WP-009: no runtime path needed a missing grant). Privileges
-- each runtime path exercises, verified through fcb_app on real PostgreSQL by
-- tests/integration/test_wp009_roles_runtime_postgres.py:
--   schema_migrations              S        bridge/schema_readiness.py evaluate()
--   oauth_authorization_codes      S I U    oauth_store.save_code / exchange_code / consume_code (UPDATE .. RETURNING)
--   oauth_access_tokens            S I      oauth_store._insert_pair / save_access_token / valid_access_token
--   oauth_refresh_tokens           S I U    oauth_store.refresh_and_issue / rotate_refresh_token
--   oauth_claude_dcr_clients       S I D    oauth_store.register_claude_dcr_client (expiry DELETE) / valid_claude_dcr_client
--   browser_session_leases         S I U    bridge/session_leases.py (claim_stale: FOR UPDATE SKIP LOCKED needs UPDATE)
--   api_football_quota_counters    S I U    quota._reserve / _mark_exhausted (INSERT .. ON CONFLICT DO UPDATE) / _snapshot
--   api_football_quota_leases      S I D    quota._reserve (expiry DELETE) / _release / _snapshot
--   api_football_quota_attempts    S I D    quota._reserve / _mark_exhausted (ON CONFLICT DO NOTHING) / _snapshot
--   sxbet_rate_attempts            S I D    bridge/sxbet/ratelimit.py _reserve
--   sxbet_rate_cooldowns           S I U D  ratelimit._reserve (expiry DELETE) / _record (UPSERT)
-- The remaining privileges of the uniform SELECT/INSERT/UPDATE/DELETE grant below are not
-- exercised by any runtime path today (narrowing them is an owner decision, see the WP-009
-- runbook). No sequence, routine or schema CREATE privilege is granted to the runtime.
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name)
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_app'::pg_catalog.name)
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_signer'::pg_catalog.name) THEN
        RAISE EXCEPTION 'FCB_REQUIRED_ROLE_MISSING';
    END IF;
END $roles$;

DO $ownership$
DECLARE obj record;
BEGIN
    FOR obj IN
        SELECT c.relkind, n.nspname, c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
        WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
          AND c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'v', 'm']::pg_catalog."char"[])
    LOOP
        IF obj.relkind OPERATOR(pg_catalog.=) 'v'::pg_catalog."char" THEN
            EXECUTE pg_catalog.format('ALTER VIEW %I.%I OWNER TO fcb_migrator', obj.nspname, obj.relname);
        ELSIF obj.relkind OPERATOR(pg_catalog.=) 'm'::pg_catalog."char" THEN
            EXECUTE pg_catalog.format('ALTER MATERIALIZED VIEW %I.%I OWNER TO fcb_migrator', obj.nspname, obj.relname);
        ELSE
            EXECUTE pg_catalog.format('ALTER TABLE %I.%I OWNER TO fcb_migrator', obj.nspname, obj.relname);
        END IF;
    END LOOP;

    -- PostgreSQL rejects ALTER SEQUENCE OWNER for SERIAL/IDENTITY sequences
    -- linked to a table. ALTER TABLE above already transfers those sequences.
    -- Transfer only independent sequences here.
    FOR obj IN
        SELECT n.nspname, c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
        WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
          AND c.relkind OPERATOR(pg_catalog.=) 'S'::pg_catalog."char"
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_depend d
              WHERE d.classid OPERATOR(pg_catalog.=) 'pg_catalog.pg_class'::pg_catalog.regclass
                AND d.objid OPERATOR(pg_catalog.=) c.oid
                AND d.deptype OPERATOR(pg_catalog.=) ANY (ARRAY['a', 'i']::pg_catalog."char"[])
          )
    LOOP
        EXECUTE pg_catalog.format('ALTER SEQUENCE %I.%I OWNER TO fcb_migrator', obj.nspname, obj.relname);
    END LOOP;

    FOR obj IN
        SELECT n.nspname, p.proname, pg_catalog.pg_get_function_identity_arguments(p.oid) AS args
        FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
        WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
          AND p.prokind OPERATOR(pg_catalog.=) ANY (ARRAY['f', 'p']::pg_catalog."char"[])
    LOOP
        EXECUTE pg_catalog.format('ALTER ROUTINE %I.%I(%s) OWNER TO fcb_migrator', obj.nspname, obj.proname, obj.args);
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
