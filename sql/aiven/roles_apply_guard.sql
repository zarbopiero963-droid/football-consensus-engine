-- FCB-WP-009: first file of the roles transaction (after the workflow's $GUARD). Re-asserts the
-- session, takes the bridge advisory lock (the restore workflow takes the same one), and aborts
-- before any write unless the database still is the restored bridge it was diagnosed as:
--   - schema_migrations holds exactly the repository's 0001-0008 (version=name=sha256 list
--     passed as :'expected_migrations', built by the workflow from sql/migrations/bridge);
--   - every object in public is owned by the session identity or by fcb_migrator (a first
--     run sees the former, an idempotent re-run the latter; any third owner refuses);
--   - no signer series object exists anywhere in the database.
-- Read-only; every catalog relation, function and operator is schema-qualified (audit F3).
DO $fcbsession$
BEGIN
    IF pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) 'public'::pg_catalog.text
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER THEN
        RAISE EXCEPTION 'FCB_ROLES_SESSION_GUARD_FAILED';
    END IF;
END $fcbsession$;
SET LOCAL search_path = public;
SELECT pg_catalog.pg_advisory_xact_lock(460244120::pg_catalog.int8);
SELECT pg_catalog.set_config('fcb.expected_migrations', :'expected_migrations', true) IS NOT NULL AS expected_set;
DO $fcbrolesguard$
DECLARE
    got pg_catalog.text;
    foreign_owned pg_catalog.int8;
    sess_oid pg_catalog.oid := (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER);
    mig_oid pg_catalog.oid := (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name);
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
                   WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
                     AND c.relname OPERATOR(pg_catalog.=) 'schema_migrations'::pg_catalog.name) THEN
        RAISE EXCEPTION 'FCB_ROLES_MIGRATIONS_GUARD_FAILED';
    END IF;
    SELECT pg_catalog.string_agg(m.version::pg_catalog.text OPERATOR(pg_catalog.||) '='::pg_catalog.text
                                 OPERATOR(pg_catalog.||) m.name::pg_catalog.text OPERATOR(pg_catalog.||) '='::pg_catalog.text
                                 OPERATOR(pg_catalog.||) m.sha256::pg_catalog.text, ' '::pg_catalog.text ORDER BY m.version)
      INTO got FROM public.schema_migrations m;
    IF got IS NULL OR got OPERATOR(pg_catalog.<>) pg_catalog.current_setting('fcb.expected_migrations') THEN
        RAISE EXCEPTION 'FCB_ROLES_MIGRATIONS_GUARD_FAILED';
    END IF;
    SELECT (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
            WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
              AND c.relowner OPERATOR(pg_catalog.<>) sess_oid AND c.relowner IS DISTINCT FROM mig_oid)
         OPERATOR(pg_catalog.+)
           (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
            WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
              AND p.proowner OPERATOR(pg_catalog.<>) sess_oid AND p.proowner IS DISTINCT FROM mig_oid)
         OPERATOR(pg_catalog.+)
           (SELECT pg_catalog.count(*) FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) t.typnamespace
            WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
              AND t.typowner OPERATOR(pg_catalog.<>) sess_oid AND t.typowner IS DISTINCT FROM mig_oid)
      INTO foreign_owned;
    IF foreign_owned OPERATOR(pg_catalog.<>) 0::pg_catalog.int8 THEN
        RAISE EXCEPTION 'FCB_ROLES_OWNER_GUARD_FAILED';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c
               WHERE c.relname OPERATOR(pg_catalog.=) ANY (ARRAY['signer_schema_migrations', 'signer_ledger_anchors',
                                                                 'signer_ledger_writer_leases']::pg_catalog.name[])) THEN
        RAISE EXCEPTION 'FCB_ROLES_SIGNER_SERIES_PRESENT';
    END IF;
END $fcbrolesguard$;
