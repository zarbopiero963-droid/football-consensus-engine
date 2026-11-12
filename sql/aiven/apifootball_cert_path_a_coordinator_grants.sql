-- PATH A2 step 1b, database `defaultdb`, ONE transaction run by the OWNER of the three shared
-- API-FOOTBALL quota-coordinator tables. After WP-009 phase 2 that owner is fcb_migrator, and the
-- install workflow requires the fcb_migrator identity (FCB_MIGRATOR_DB_URL); the administrator no
-- longer owns these tables and cannot grant on them. Run after apifootball_cert_path_a_roles.sql
-- (step 1, administrator: roles, runner password, explicit USAGE on public) and followed, in the
-- same transaction, by apifootball_cert_path_a_coordinator_assert.sql (exactly these 9 privileges).
-- The grants are EXACTLY the 9 coordinator privileges (8 table-level + 1 column-level), without
-- grant option; nothing else. Idempotent. Catalog-qualified. Never run for a password rotation.
SET LOCAL search_path = pg_catalog, public;
DO $fcbcertgrantguard$
BEGIN
    IF pg_catalog.current_database() OPERATOR(pg_catalog.<>) 'defaultdb'::pg_catalog.name
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER
       OR (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER)
       OR SESSION_USER OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner', 'fcb_app', 'fcb_signer']::pg_catalog.name[])
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name)
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_roles o ON o.oid OPERATOR(pg_catalog.=) c.relowner
           WHERE c.relnamespace OPERATOR(pg_catalog.=) 'public'::pg_catalog.regnamespace
             AND c.relkind OPERATOR(pg_catalog.=) 'r'::pg_catalog."char"
             AND o.rolname OPERATOR(pg_catalog.=) SESSION_USER
             AND c.relname OPERATOR(pg_catalog.=) ANY (ARRAY['api_football_quota_counters', 'api_football_quota_leases',
                                                             'api_football_quota_attempts']::pg_catalog.name[]))
          OPERATOR(pg_catalog.<>) 3::pg_catalog.int8 THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_GRANTS_GUARD_FAILED';
    END IF;
END $fcbcertgrantguard$;
GRANT SELECT, INSERT ON TABLE public.api_football_quota_counters TO fcb_cert_runner;
GRANT UPDATE (used) ON TABLE public.api_football_quota_counters TO fcb_cert_runner;
GRANT SELECT, INSERT, DELETE ON TABLE public.api_football_quota_leases, public.api_football_quota_attempts TO fcb_cert_runner;
