-- AF-07 API-FOOTBALL certification campaign ledger: runs in the SAME transaction as
-- aiven_migrations_bridge_0001_0010.sql, as fcb_migrator (the bridge owner after WP-009 phase 2).
-- The campaign runner connects with the coordinator DSN (APIFOOTBALL_COORDINATOR_DATABASE_URL); its
-- role is assumed to be fcb_app, the role that already holds the api_football_quota_* privileges
-- (AF-12 owner decision 2026-11-11: fcb_app with minimal privileges; fcb_app is SHARED with the
-- other runtime components, a dedicated role is a future objective). Append-only: SELECT and INSERT
-- only; no UPDATE, DELETE, TRUNCATE, REFERENCES or TRIGGER (the 0010 triggers also refuse UPDATE,
-- DELETE and TRUNCATE for every role). fcb_app does not own the tables and is not a member of
-- fcb_migrator, so it can neither ALTER them nor DISABLE their triggers. The same conditional grant
-- is in the WP-009 files (grant_bridge_runtime.sql, finalize_bridge_privileges.sql), so a re-run of
-- the roles workflow keeps it. Catalog-qualified.
--   api_football_cert_campaigns          S I   open a campaign (INSERT ... ON CONFLICT DO NOTHING), read budget
--   api_football_cert_campaign_attempts  S I   write-ahead reservation, count, attempted call ids
--   api_football_cert_campaign_outcomes  S I   outcome of each reserved attempt (append-only)
DO $who$
BEGIN
    IF CURRENT_USER OPERATOR(pg_catalog.<>) 'fcb_migrator'::pg_catalog.name
       OR SESSION_USER OPERATOR(pg_catalog.<>) 'fcb_migrator'::pg_catalog.name THEN
        RAISE EXCEPTION 'FCB_CERT_LEDGER_NOT_MIGRATOR';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_app'::pg_catalog.name) THEN
        RAISE EXCEPTION 'FCB_REQUIRED_ROLE_MISSING';
    END IF;
END $who$;

REVOKE ALL ON TABLE public.api_football_cert_campaigns, public.api_football_cert_campaign_attempts,
    public.api_football_cert_campaign_outcomes FROM PUBLIC;
REVOKE ALL ON FUNCTION public.api_football_cert_reject_mutation(), public.api_football_cert_attempt_guard() FROM PUBLIC;
GRANT SELECT, INSERT ON TABLE public.api_football_cert_campaigns, public.api_football_cert_campaign_attempts, public.api_football_cert_campaign_outcomes TO fcb_app;

DO $assert$
DECLARE t pg_catalog.text;
BEGIN
    IF pg_catalog.pg_has_role('fcb_app', 'fcb_migrator', 'MEMBER') THEN
        RAISE EXCEPTION 'FCB_CERT_LEDGER_OWNER_UNEXPECTED';
    END IF;
    FOREACH t IN ARRAY ARRAY['public.api_football_cert_campaigns', 'public.api_football_cert_campaign_attempts',
                             'public.api_football_cert_campaign_outcomes']::pg_catalog.text[] LOOP
        IF (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid OPERATOR(pg_catalog.=) t::pg_catalog.regclass)
             OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name) THEN
            RAISE EXCEPTION 'FCB_CERT_LEDGER_OWNER_UNEXPECTED';
        END IF;
        IF NOT pg_catalog.has_table_privilege('fcb_app', t, 'SELECT')
           OR NOT pg_catalog.has_table_privilege('fcb_app', t, 'INSERT')
           OR pg_catalog.has_table_privilege('fcb_app', t, 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege('fcb_app', t, 'UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'FCB_CERT_LEDGER_GRANTS_UNEXPECTED';
        END IF;
    END LOOP;
END $assert$;
