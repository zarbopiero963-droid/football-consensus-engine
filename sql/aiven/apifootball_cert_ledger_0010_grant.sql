-- AF-07 API-FOOTBALL certification campaign ledger: runs in the SAME transaction as
-- aiven_migrations_bridge_0001_0010.sql, as fcb_migrator (the bridge owner after WP-009 phase 2).
-- The campaign runner connects with the coordinator DSN (APIFOOTBALL_COORDINATOR_DATABASE_URL); its
-- role is assumed to be fcb_app, the role that already holds the api_football_quota_* privileges
-- (OWNER DECISION: a dedicated role would be stricter). Append-only: SELECT and INSERT only, no
-- UPDATE, DELETE or TRUNCATE (the 0010 triggers also refuse them). Catalog-qualified.
--   api_football_cert_campaigns          S I   open a campaign (INSERT ... ON CONFLICT DO NOTHING), read budget
--   api_football_cert_campaign_attempts  S I   write-ahead reservation, count, attempted call ids
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

REVOKE ALL ON TABLE public.api_football_cert_campaigns, public.api_football_cert_campaign_attempts FROM PUBLIC;
REVOKE ALL ON FUNCTION public.api_football_cert_reject_mutation(), public.api_football_cert_attempt_guard() FROM PUBLIC;
GRANT SELECT, INSERT ON TABLE public.api_football_cert_campaigns, public.api_football_cert_campaign_attempts TO fcb_app;

DO $assert$
BEGIN
    IF (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid OPERATOR(pg_catalog.=) 'public.api_football_cert_campaigns'::pg_catalog.regclass)
         OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name)
       OR (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid OPERATOR(pg_catalog.=) 'public.api_football_cert_campaign_attempts'::pg_catalog.regclass)
         OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name) THEN
        RAISE EXCEPTION 'FCB_CERT_LEDGER_OWNER_UNEXPECTED';
    END IF;
    IF NOT pg_catalog.has_table_privilege('fcb_app', 'public.api_football_cert_campaigns', 'SELECT,INSERT')
       OR NOT pg_catalog.has_table_privilege('fcb_app', 'public.api_football_cert_campaign_attempts', 'SELECT,INSERT')
       OR pg_catalog.has_table_privilege('fcb_app', 'public.api_football_cert_campaigns', 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_table_privilege('fcb_app', 'public.api_football_cert_campaign_attempts', 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
        RAISE EXCEPTION 'FCB_CERT_LEDGER_GRANTS_UNEXPECTED';
    END IF;
END $assert$;
