-- FCB-WP-009: first file of the roles transaction (after the workflow's $GUARD). Re-asserts the
-- session, takes the bridge advisory lock (the restore workflow takes the same one), then decides
-- the path (psql variable :fcb_first_run, also read by sql/aiven/roles_post_checks.sql):
--
-- FIRST RUN (none of fcb_migrator, fcb_app, fcb_signer exists): the database must still be the
--   restored bridge, owned only by the session identity (Aiven's avnadmin):
--   - schema_migrations holds exactly the repository's 0001-0008 (:'expected_migrations');
--   - every object in public is owned by the session identity (no fcb_migrator yet, so no code
--     another role could have planted can run in this read-write administrative transaction).
--
-- RE-RUN (any of the three roles exists): the model must already be exactly HARDENED
--   (sql/aiven/verify_bridge_roles.sql, which also pins every routine, trigger, type, rule,
--   policy and relation kind of public) or the transaction aborts here, before any write.
--   On this path the administrative identity NEVER reads or writes a bridge table: such access
--   could execute code fcb_migrator (the owner) planted - triggers, forced row-level policies,
--   defaults - with the administrator's rights, and SET ROLE is no boundary (the code could
--   RESET ROLE). Every later statement of the transaction is catalog-only DDL (ALTER ROLE,
--   GRANT/REVOKE, ALTER ... OWNER, ALTER DEFAULT PRIVILEGES), which runs no user code; event
--   triggers, the only DDL hook, need a superuser and are refused by the model.
--
-- Both paths: no signer series object anywhere in the database.
-- Every catalog relation, function and operator is schema-qualified (audit F3).
DO $fcbsession$
BEGIN
    IF pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) 'public'::pg_catalog.text
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER THEN
        RAISE EXCEPTION 'FCB_ROLES_SESSION_GUARD_FAILED';
    END IF;
END $fcbsession$;
SET LOCAL search_path = public;
SELECT pg_catalog.pg_advisory_xact_lock(460244120::pg_catalog.int8);
DO $fcbsigner$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c
               WHERE c.relname OPERATOR(pg_catalog.=) ANY (ARRAY['signer_schema_migrations', 'signer_ledger_anchors',
                                                                 'signer_ledger_writer_leases']::pg_catalog.name[])) THEN
        RAISE EXCEPTION 'FCB_ROLES_SIGNER_SERIES_PRESENT';
    END IF;
END $fcbsigner$;
SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
                   WHERE r.rolname OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_migrator', 'fcb_app', 'fcb_signer']::pg_catalog.name[]))
       AS first_run \gset fcb_
\if :fcb_first_run
SELECT pg_catalog.set_config('fcb.expected_migrations', :'expected_migrations', true) IS NOT NULL AS expected_set;
DO $fcbrolesguard$
DECLARE
    got pg_catalog.text;
    foreign_owned pg_catalog.int8;
    sess_oid pg_catalog.oid := (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER);
BEGIN
    SELECT (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
            WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND c.relowner OPERATOR(pg_catalog.<>) sess_oid)
         OPERATOR(pg_catalog.+)
           (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
            WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND p.proowner OPERATOR(pg_catalog.<>) sess_oid)
         OPERATOR(pg_catalog.+)
           (SELECT pg_catalog.count(*) FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) t.typnamespace
            WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND t.typowner OPERATOR(pg_catalog.<>) sess_oid)
      INTO foreign_owned;
    IF foreign_owned OPERATOR(pg_catalog.<>) 0::pg_catalog.int8 THEN
        RAISE EXCEPTION 'FCB_ROLES_OWNER_GUARD_FAILED';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
                   WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
                     AND c.relname OPERATOR(pg_catalog.=) 'schema_migrations'::pg_catalog.name) THEN
        RAISE EXCEPTION 'FCB_ROLES_MIGRATIONS_GUARD_FAILED';
    END IF;
    -- Safe to read: every object is owned by this session (checked above), nobody else's code.
    SELECT pg_catalog.string_agg(m.version::pg_catalog.text OPERATOR(pg_catalog.||) '='::pg_catalog.text
                                 OPERATOR(pg_catalog.||) m.name::pg_catalog.text OPERATOR(pg_catalog.||) '='::pg_catalog.text
                                 OPERATOR(pg_catalog.||) m.sha256::pg_catalog.text, ' '::pg_catalog.text ORDER BY m.version)
      INTO got FROM public.schema_migrations m;
    IF got IS NULL OR got OPERATOR(pg_catalog.<>) pg_catalog.current_setting('fcb.expected_migrations') THEN
        RAISE EXCEPTION 'FCB_ROLES_MIGRATIONS_GUARD_FAILED';
    END IF;
END $fcbrolesguard$;
\else
\ir verify_bridge_roles.sql
SELECT pg_catalog.set_config('fcb.roles_state_before', :'fcb_roles_state', true) IS NOT NULL AS state_set;
DO $fcbrerun$
BEGIN
    IF pg_catalog.current_setting('fcb.roles_state_before') OPERATOR(pg_catalog.<>) 'HARDENED'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_ROLES_RERUN_NOT_HARDENED %', pg_catalog.current_setting('fcb.roles_state_before');
    END IF;
END $fcbrerun$;
\endif
