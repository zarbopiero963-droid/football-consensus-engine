-- FCB-WP-009 PHASE 2 (workflow mode=enable-migrator): ONE transaction, after the workflow's $GUARD
-- and only once the owner has switched the runtime to fcb_app and an authenticated MCP E2E run
-- proved it (checked by the workflow before this file runs). Then, atomically:
--   1. re-assert the session; take the bridge advisory lock without waiting;
--   2. require the model to be HARDENED_PHASE1 (first run) or HARDENED (re-run: password only),
--      from the catalog only;
--   3. refuse while any OTHER backend of the administrative identity is connected to this
--      database (observed :'observe_seconds' seconds: the runtime must no longer use it);
--   4. first run only: give the administrator INHERIT+SET on fcb_migrator for THIS transaction,
--      run prepare (fcb_migrator schema rights, its default privileges) and finalize (ownership
--      of every bridge object to fcb_migrator, runtime allowlist), then REVOKE that membership:
--      afterwards the administrator keeps only PostgreSQL's automatic ADMIN-only grant (no
--      INHERIT, no SET), so it holds NO privilege on any bridge table and cannot run code
--      fcb_migrator may attach to them (any access fails with permission denied first);
--   5. enable fcb_migrator LOGIN with the owner's password (verifier via \getenv).
-- Nobody could act as fcb_migrator before this commit (NOLOGIN, owned nothing, no member could
-- SET to it), and nobody can make the administrator run fcb_migrator's code after it. There is
-- no window in between: both changes commit together.
-- The administrator reads no bridge table in this transaction (catalog only).
\getenv fcb_migrator_verifier FCB_MIGRATOR_DB_VERIFIER
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
SELECT pg_catalog.set_config('fcb.verifier_ok',
    (:'fcb_migrator_verifier'::pg_catalog.text OPERATOR(pg_catalog.~) '^SCRAM-SHA-256\$4096:[A-Za-z0-9+/]+=*\$[A-Za-z0-9+/]+=*:[A-Za-z0-9+/]+=*$'::pg_catalog.text)::pg_catalog.text,
    true) IS NOT NULL AS verifier_checked;
DO $fcbverifier$
BEGIN
    IF pg_catalog.current_setting('fcb.verifier_ok') OPERATOR(pg_catalog.<>) 'true'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_ROLE_VERIFIER_INVALID';
    END IF;
END $fcbverifier$;
\ir verify_bridge_roles.sql
SELECT pg_catalog.set_config('fcb.roles_state_before', :'fcb_roles_state', true) IS NOT NULL AS state_set;
DO $fcbstate$
BEGIN
    IF pg_catalog.current_setting('fcb.roles_state_before') OPERATOR(pg_catalog.<>) ALL (ARRAY['HARDENED_PHASE1', 'HARDENED']::pg_catalog.text[]) THEN
        RAISE EXCEPTION 'FCB_ROLES_PHASE2_STATE_REFUSED %', pg_catalog.current_setting('fcb.roles_state_before');
    END IF;
END $fcbstate$;
SELECT (:'fcb_roles_state' OPERATOR(pg_catalog.=) 'HARDENED_PHASE1'::pg_catalog.text) AS phase2_first_run \gset fcb_
SELECT pg_catalog.set_config('fcb.observe_seconds', :'observe_seconds', true) IS NOT NULL AS observe_set;
DO $fcbsessions$
DECLARE
    others pg_catalog.int8;
    remaining pg_catalog.int4 := pg_catalog.current_setting('fcb.observe_seconds')::pg_catalog.int4;
BEGIN
    LOOP
        PERFORM pg_catalog.pg_stat_clear_snapshot();
        SELECT pg_catalog.count(*) INTO others FROM pg_catalog.pg_stat_activity a
        WHERE a.usename OPERATOR(pg_catalog.=) SESSION_USER
          AND a.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()
          AND a.pid OPERATOR(pg_catalog.<>) pg_catalog.pg_backend_pid();
        IF others OPERATOR(pg_catalog.>) 0::pg_catalog.int8 THEN
            RAISE EXCEPTION 'FCB_ROLES_ADMIN_SESSION_PRESENT %', others;
        END IF;
        EXIT WHEN remaining OPERATOR(pg_catalog.<=) 0;
        PERFORM pg_catalog.pg_sleep(1);
        remaining := remaining OPERATOR(pg_catalog.-) 1;
    END LOOP;
END $fcbsessions$;
\if :fcb_phase2_first_run
GRANT fcb_migrator TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;
\ir prepare_bridge_roles.sql
\ir finalize_bridge_privileges.sql
REVOKE fcb_migrator FROM CURRENT_USER;
\endif
ALTER ROLE fcb_migrator WITH LOGIN PASSWORD :'fcb_migrator_verifier';
