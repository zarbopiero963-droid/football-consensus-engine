-- FCB-WP-009 PHASE 1 guard: first file of the phase-1 transaction (after the workflow's $GUARD).
-- Re-asserts the session, takes the bridge advisory lock WITHOUT waiting (any login can hold an
-- advisory lock id, so a held lock is refused fast with FCB_ROLES_LOCK_BUSY instead of hanging;
-- the GitHub concurrency group already serializes this workflow with the restore), then aborts
-- before any write unless:
--   - no signer series object exists anywhere in the database;
--   - every object in public is owned by the session identity (phase 1 never moves ownership;
--     fcb_migrator owns nothing anywhere in this database);
--   - the bridge roles are absent (first run) or already exactly HARDENED_PHASE1 (re-run);
--   - schema_migrations holds exactly the repository's 0001-0008 (:'expected_migrations').
-- Reading schema_migrations here is the ONE administrative row read of the design, and it is
-- safe by construction: this transaction just proved that the administrative identity owns every
-- object of public and that no other non-superuser role can act as it or as fcb_migrator
-- (verify_bridge_roles.sql: role_reachability), so only code the administrator itself wrote can
-- be attached to the table. After phase 2 the administrator holds no privilege on any bridge
-- table and never reads one.
-- Every catalog relation, function and operator is schema-qualified (audit F3).
DO $fcbsession$
BEGIN
    IF pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) 'public'::pg_catalog.text
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER THEN
        RAISE EXCEPTION 'FCB_ROLES_SESSION_GUARD_FAILED';
    END IF;
END $fcbsession$;
SET LOCAL search_path = public;
SELECT pg_catalog.set_config('fcb.lock_taken',
    pg_catalog.pg_try_advisory_xact_lock(460244120::pg_catalog.int8)::pg_catalog.text, true) IS NOT NULL AS lock_tried;
DO $fcblock$
BEGIN
    IF pg_catalog.current_setting('fcb.lock_taken') OPERATOR(pg_catalog.<>) 'true'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_ROLES_LOCK_BUSY';
    END IF;
END $fcblock$;
DO $fcbsigner$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c
               WHERE c.relname OPERATOR(pg_catalog.=) ANY (ARRAY['signer_schema_migrations', 'signer_ledger_anchors',
                                                                 'signer_ledger_writer_leases']::pg_catalog.name[])) THEN
        RAISE EXCEPTION 'FCB_ROLES_SIGNER_SERIES_PRESENT';
    END IF;
END $fcbsigner$;
DO $fcbowners$
DECLARE
    sess_oid pg_catalog.oid := (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER);
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
               WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND c.relowner OPERATOR(pg_catalog.<>) sess_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
               WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND p.proowner OPERATOR(pg_catalog.<>) sess_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) t.typnamespace
               WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND t.typowner OPERATOR(pg_catalog.<>) sess_oid) THEN
        RAISE EXCEPTION 'FCB_ROLES_OWNER_GUARD_FAILED';
    END IF;
END $fcbowners$;
SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
                   WHERE r.rolname OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_migrator', 'fcb_app', 'fcb_signer']::pg_catalog.name[]))
       AS first_run \gset fcb_
\if :fcb_first_run
\else
\ir verify_bridge_roles.sql
SELECT pg_catalog.set_config('fcb.roles_state_before', :'fcb_roles_state', true) IS NOT NULL AS state_set;
DO $fcbrerun$
BEGIN
    IF pg_catalog.current_setting('fcb.roles_state_before') OPERATOR(pg_catalog.<>) 'HARDENED_PHASE1'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_ROLES_RERUN_NOT_HARDENED %', pg_catalog.current_setting('fcb.roles_state_before');
    END IF;
END $fcbrerun$;
\endif
SELECT pg_catalog.set_config('fcb.expected_migrations', :'expected_migrations', true) IS NOT NULL AS expected_set;
DO $fcbmigrations$
DECLARE got pg_catalog.text;
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
END $fcbmigrations$;
