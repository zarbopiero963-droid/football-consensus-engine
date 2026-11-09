-- FCB-WP-009: create or update the three bridge roles. Runs ONLY inside the owner-gated roles
-- transaction (.github/workflows/fcb-aiven-defaultdb-roles.yml, mode=apply), as the
-- administrative identity (Aiven's avnadmin: no superuser, CREATEROLE), after
-- sql/aiven/roles_apply_guard.sql and before prepare_bridge_roles.sql /
-- finalize_bridge_privileges.sql.
--
-- No credential is stored here. The two LOGIN roles receive SCRAM-SHA-256 verifiers that
-- scripts/fcb_db_roles.py computes on the runner from the owner's Environment secrets; psql
-- reads them from its own environment with \getenv (never from a command line), so neither
-- the plaintext nor the verifier appears in argv, and the server never sees the plaintext.
-- An unset variable leaves the literal :'name' in the statement: a syntax error, nothing
-- committed. fcb_signer stays NOLOGIN with no password (the isolated signer is not deployed).
--
-- Role attributes are not set here: a role created by a non-superuser can never be SUPERUSER,
-- REPLICATION or BYPASSRLS, and the final model check (sql/aiven/verify_bridge_roles.sql)
-- refuses any of those, CREATEDB, CREATEROLE, or any unexpected membership, before commit.
--
-- Membership: the administrative identity gets SET + INHERIT on fcb_migrator. SET is required
-- to hand the bridge objects over (ALTER ... OWNER TO), INHERIT to change fcb_migrator's
-- default privileges, and INHERIT also keeps the previous runtime DSN working on the
-- migrator-owned tables, which is the rollback path of the runbook.
\getenv fcb_app_verifier FCB_APP_DB_VERIFIER
\getenv fcb_migrator_verifier FCB_MIGRATOR_DB_VERIFIER
DO $fcbsession$
BEGIN
    IF pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) 'public'::pg_catalog.text
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER THEN
        RAISE EXCEPTION 'FCB_ROLES_SESSION_GUARD_FAILED';
    END IF;
END $fcbsession$;
SELECT pg_catalog.set_config('fcb.verifiers_ok',
    ((:'fcb_app_verifier'::pg_catalog.text OPERATOR(pg_catalog.~) '^SCRAM-SHA-256\$4096:[A-Za-z0-9+/]+=*\$[A-Za-z0-9+/]+=*:[A-Za-z0-9+/]+=*$'::pg_catalog.text)
     AND (:'fcb_migrator_verifier'::pg_catalog.text OPERATOR(pg_catalog.~) '^SCRAM-SHA-256\$4096:[A-Za-z0-9+/]+=*\$[A-Za-z0-9+/]+=*:[A-Za-z0-9+/]+=*$'::pg_catalog.text)
     AND (:'fcb_app_verifier'::pg_catalog.text OPERATOR(pg_catalog.<>) :'fcb_migrator_verifier'::pg_catalog.text))::pg_catalog.text,
    true) IS NOT NULL AS verifiers_checked;
DO $fcbverifiers$
BEGIN
    IF pg_catalog.current_setting('fcb.verifiers_ok') OPERATOR(pg_catalog.<>) 'true'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_ROLE_VERIFIER_INVALID';
    END IF;
END $fcbverifiers$;
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
ALTER ROLE fcb_migrator WITH LOGIN PASSWORD :'fcb_migrator_verifier';
ALTER ROLE fcb_app WITH LOGIN PASSWORD :'fcb_app_verifier';
ALTER ROLE fcb_signer WITH NOLOGIN PASSWORD NULL;
GRANT fcb_migrator TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;
