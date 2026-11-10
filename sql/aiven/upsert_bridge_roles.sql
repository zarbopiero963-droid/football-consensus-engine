-- FCB-WP-009 PHASE 1 (workflow mode=apply): create or update the three bridge roles. Runs ONLY
-- inside the owner-gated phase-1 transaction (.github/workflows/fcb-aiven-defaultdb-roles.yml), as
-- the administrative identity (Aiven's avnadmin: no superuser, CREATEROLE), after
-- sql/aiven/roles_apply_guard.sql and before sql/aiven/grant_bridge_runtime.sql.
--
-- Phase 1 leaves every bridge object owned by the administrative identity (status quo) and
-- creates:
--   fcb_app       LOGIN, password from the owner's Environment secret (SCRAM-SHA-256 verifier
--                 computed on the runner by scripts/fcb_db_roles.py, read here with \getenv:
--                 never on a command line; an unset variable is a syntax error, nothing commits);
--   fcb_migrator  NOLOGIN, no password, owns nothing, no membership: nobody can act as it until
--                 phase 2 (sql/aiven/enable_bridge_migrator.sql) hands it the bridge in the
--                 same transaction that enables its login;
--   fcb_signer    NOLOGIN, no password (the isolated signer is not deployed).
-- No membership is granted here. Role attributes are not set: a role created by a non-superuser
-- can never be SUPERUSER, REPLICATION or BYPASSRLS, and the model check refuses those, CREATEDB,
-- CREATEROLE and any unexpected membership before commit.
\getenv fcb_app_verifier FCB_APP_DB_VERIFIER
DO $fcbsession$
BEGIN
    IF pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) 'public'::pg_catalog.text
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER THEN
        RAISE EXCEPTION 'FCB_ROLES_SESSION_GUARD_FAILED';
    END IF;
END $fcbsession$;
SELECT pg_catalog.set_config('fcb.verifiers_ok',
    (:'fcb_app_verifier'::pg_catalog.text OPERATOR(pg_catalog.~) '^SCRAM-SHA-256\$4096:[A-Za-z0-9+/]+=*\$[A-Za-z0-9+/]+=*:[A-Za-z0-9+/]+=*$'::pg_catalog.text)::pg_catalog.text,
    true) IS NOT NULL AS verifiers_checked;
DO $fcbverifiers$
BEGIN
    IF pg_catalog.current_setting('fcb.verifiers_ok') OPERATOR(pg_catalog.<>) 'true'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_ROLE_VERIFIER_INVALID';
    END IF;
END $fcbverifiers$;
-- Aiven runs with createrole_self_grant = 'inherit,set' (server level, verified read-only on
-- defaultdb): every role avnadmin creates would also be granted to avnadmin WITH INHERIT and
-- SET, so the administrative identity could act as fcb_app, fcb_signer and fcb_migrator. Turn
-- it off for this transaction only (user-settable GUC, source 'session', so the target guard's
-- database/user-settings check is unaffected) and prove it before any CREATE ROLE. The creator
-- then keeps only PostgreSQL's automatic ADMIN-only membership (no INHERIT, no SET).
SET LOCAL createrole_self_grant = '';
DO $fcbselfgrant$
BEGIN
    IF pg_catalog.current_setting('createrole_self_grant') OPERATOR(pg_catalog.<>) ''::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_ROLES_SELF_GRANT_NOT_DISABLED';
    END IF;
END $fcbselfgrant$;
DO $fcbcreate$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name) THEN
        CREATE ROLE fcb_migrator NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_app'::pg_catalog.name) THEN
        CREATE ROLE fcb_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_signer'::pg_catalog.name) THEN
        CREATE ROLE fcb_signer NOLOGIN;
    END IF;
END $fcbcreate$;
ALTER ROLE fcb_migrator WITH NOLOGIN PASSWORD NULL;
ALTER ROLE fcb_app WITH LOGIN PASSWORD :'fcb_app_verifier';
ALTER ROLE fcb_signer WITH NOLOGIN PASSWORD NULL;
