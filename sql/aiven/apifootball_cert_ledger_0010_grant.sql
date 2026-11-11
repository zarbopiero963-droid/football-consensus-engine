-- AF-07 API-FOOTBALL certification campaign ledger: runs in the SAME transaction as
-- aiven_migrations_bridge_0001_0010.sql, as fcb_migrator (the bridge owner after WP-009 phase 2).
-- The campaign runner connects with the coordinator DSN (APIFOOTBALL_COORDINATOR_DATABASE_URL); its
-- role is assumed to be fcb_app, the role that already holds the api_football_quota_* privileges
-- (AF-12 owner decision 2026-11-11: fcb_app with minimal privileges; fcb_app is SHARED with the
-- other runtime components, a dedicated role is a future objective). Append-only: SELECT on the
-- tables and INSERT on the writer's columns ONLY (none at all on api_football_cert_plans: AF-15) (column-level: never the audit timestamps
-- created_at / reserved_at / recorded_at, which the 0010 triggers also stamp server-side); no
-- UPDATE, DELETE, TRUNCATE, REFERENCES or TRIGGER (the 0010 triggers also refuse UPDATE, DELETE and
-- TRUNCATE for every role). fcb_app does not own the tables and is not a member of fcb_migrator, so
-- it can neither ALTER them nor DISABLE their triggers. Catalog-qualified.
-- The same conditional grant is in the WP-009 files (grant_bridge_runtime.sql,
-- finalize_bridge_privileges.sql), but NOTE: the WP-009 roles workflow today refuses any database
-- whose schema_migrations differs from 0001-0008 (pinned counts, rows and verify model), so that
-- block is NOT reached through the workflow on a 0009/0010 database until the WP-009 verify model
-- is updated (dedicated PR, owner decision). Until then this file is the only grant path.
--   api_football_cert_plans              S                                              read the approved plan budget (AF-15: rows
--                                                                                      written ONLY by the plan approval workflow as fcb_migrator)
--   api_football_cert_campaigns          S, I(campaign_id, plan_sha256)                 open (ON CONFLICT DO NOTHING)
--   api_football_cert_campaign_attempts  S, I(campaign_id, seq, call_id)               write-ahead reservation, count, attempted calls
--   api_football_cert_campaign_outcomes  S, I(campaign_id, seq, outcome, http_status)  outcome of each reserved attempt
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

REVOKE ALL ON TABLE public.api_football_cert_plans, public.api_football_cert_campaigns,
    public.api_football_cert_campaign_attempts, public.api_football_cert_campaign_outcomes FROM PUBLIC, fcb_app;
REVOKE ALL ON FUNCTION public.api_football_cert_reject_mutation(), public.api_football_cert_attempt_guard(),
    public.api_football_cert_stamp() FROM PUBLIC;
GRANT SELECT ON TABLE public.api_football_cert_plans, public.api_football_cert_campaigns, public.api_football_cert_campaign_attempts, public.api_football_cert_campaign_outcomes TO fcb_app;
GRANT INSERT (campaign_id, plan_sha256) ON TABLE public.api_football_cert_campaigns TO fcb_app;
GRANT INSERT (campaign_id, seq, call_id) ON TABLE public.api_football_cert_campaign_attempts TO fcb_app;
GRANT INSERT (campaign_id, seq, outcome, http_status) ON TABLE public.api_football_cert_campaign_outcomes TO fcb_app;

DO $assert$
DECLARE
    tables pg_catalog.text[] := ARRAY['public.api_football_cert_plans', 'public.api_football_cert_campaigns',
                                      'public.api_football_cert_campaign_attempts',
                                      'public.api_football_cert_campaign_outcomes']::pg_catalog.text[];
    writable pg_catalog.text[] := ARRAY['', 'campaign_id,plan_sha256', 'campaign_id,seq,call_id',
                                        'campaign_id,seq,outcome,http_status']::pg_catalog.text[];
    i pg_catalog.int4;
BEGIN
    IF pg_catalog.pg_has_role('fcb_app', 'fcb_migrator', 'MEMBER') THEN
        RAISE EXCEPTION 'FCB_CERT_LEDGER_OWNER_UNEXPECTED';
    END IF;
    FOR i IN 1..4 LOOP
        IF (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid OPERATOR(pg_catalog.=) tables[i]::pg_catalog.regclass)
             OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name) THEN
            RAISE EXCEPTION 'FCB_CERT_LEDGER_OWNER_UNEXPECTED';
        END IF;
        -- table level: SELECT only; INSERT exactly on the writer's columns (never an audit timestamp)
        IF NOT pg_catalog.has_table_privilege('fcb_app', tables[i], 'SELECT')
           OR pg_catalog.has_table_privilege('fcb_app', tables[i], 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege('fcb_app', tables[i], 'UPDATE,REFERENCES')
           OR (SELECT COALESCE(pg_catalog.string_agg(a.attname::pg_catalog.text, ','::pg_catalog.text ORDER BY a.attnum), ''::pg_catalog.text)
               FROM pg_catalog.pg_attribute a
               WHERE a.attrelid OPERATOR(pg_catalog.=) tables[i]::pg_catalog.regclass
                 AND a.attnum OPERATOR(pg_catalog.>) 0 AND NOT a.attisdropped
                 AND pg_catalog.has_column_privilege('fcb_app', a.attrelid, a.attnum, 'INSERT'))
              IS DISTINCT FROM writable[i] THEN
            RAISE EXCEPTION 'FCB_CERT_LEDGER_GRANTS_UNEXPECTED';
        END IF;
    END LOOP;
END $assert$;
