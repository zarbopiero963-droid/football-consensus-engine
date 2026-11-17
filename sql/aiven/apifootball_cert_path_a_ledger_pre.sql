-- PATH A step 3 of 3 (first part), database `fcb_cert` (owned by fcb_cert_owner, created by step 2).
-- ONE transaction with the generated 0010 block and apifootball_cert_path_a_ledger_post.sql, run by
-- the administrative identity (avnadmin). The administrator holds only PostgreSQL's automatic
-- ADMIN-only membership in fcb_cert_owner; for THIS transaction it grants itself SET (no INHERIT),
-- creates every ledger object AS fcb_cert_owner, and the post part revokes that grant and asserts
-- it is gone before COMMIT: on any error the whole transaction, the grant included, rolls back.
-- The ledger DDL is bridge migration 0010 byte-for-byte (pinned SHA-256), in this database's
-- `public` schema, tracked in apifootball_cert_schema_migrations (NOT in defaultdb's
-- schema_migrations: the bridge series of defaultdb stays at 0001-0008, so no 0009 is needed).
SET LOCAL search_path = public;
DO $fcbcertguard$
BEGIN
    IF pg_catalog.current_database() OPERATOR(pg_catalog.<>) 'fcb_cert'::pg_catalog.name
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER
       OR (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER)
       OR SESSION_USER OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner']::pg_catalog.name[])
       OR (SELECT o.rolname FROM pg_catalog.pg_database d JOIN pg_catalog.pg_roles o ON o.oid OPERATOR(pg_catalog.=) d.datdba
           WHERE d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()) IS DISTINCT FROM 'fcb_cert_owner'::pg_catalog.name
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name)
       OR pg_catalog.pg_has_role(SESSION_USER, 'fcb_cert_owner', 'SET')
       OR pg_catalog.pg_has_role(SESSION_USER, 'fcb_cert_owner', 'USAGE') THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_LEDGER_GUARD_FAILED';
    END IF;
END $fcbcertguard$;
GRANT fcb_cert_owner TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;
SET LOCAL ROLE fcb_cert_owner;
REVOKE ALL ON DATABASE fcb_cert FROM PUBLIC;
GRANT CONNECT ON DATABASE fcb_cert TO fcb_cert_runner;
DO $fcbcertconnect$
BEGIN
    -- the administrator may connect (approval, verification) but holds no privilege on any object
    EXECUTE pg_catalog.format('GRANT CONNECT ON DATABASE fcb_cert TO %I', SESSION_USER);
END $fcbcertconnect$;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO fcb_cert_runner;
CREATE TABLE IF NOT EXISTS public.apifootball_cert_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
DO $fcbcertonly0010$
BEGIN
    IF EXISTS (SELECT 1 FROM public.apifootball_cert_schema_migrations m WHERE m.version OPERATOR(pg_catalog.<>) 10) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_UNEXPECTED_LEDGER_VERSION';
    END IF;
END $fcbcertonly0010$;
