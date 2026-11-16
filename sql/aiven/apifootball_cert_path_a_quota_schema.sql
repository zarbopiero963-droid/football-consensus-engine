-- PATH A2 step 1a, variant R1, database `defaultdb`, run by the ADMINISTRATIVE identity (Aiven's
-- avnadmin: no superuser, CREATEROLE, CREATE on the database) in ONE transaction, after
-- apifootball_cert_path_a_roles.sql. Creates the EMPTY schema fcb_cert_quota OWNED BY the owner of the
-- three shared quota-coordinator tables (after WP-009 phase 2 fcb_migrator, which has no CREATE on the
-- database: the administrator, member of fcb_migrator WITH SET, creates it on its behalf). Nothing else:
-- no USAGE for anybody (step 1b grants it to fcb_cert_runner), PUBLIC has nothing on it. Idempotent: an
-- existing fcb_cert_quota with any other owner is refused (never re-owned). Catalog-qualified.
SET LOCAL search_path = pg_catalog, pg_temp;
DO $fcbcertquotaschema$
DECLARE
    tables_owner pg_catalog.name := (
        SELECT pg_catalog.min(o.rolname::pg_catalog.text)::pg_catalog.name
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_roles o ON o.oid OPERATOR(pg_catalog.=) c.relowner
        WHERE c.relnamespace OPERATOR(pg_catalog.=) 'public'::pg_catalog.regnamespace
          AND c.relkind OPERATOR(pg_catalog.=) 'r'::pg_catalog."char"
          AND c.relname OPERATOR(pg_catalog.=) ANY (ARRAY['api_football_quota_counters', 'api_football_quota_leases',
                                                          'api_football_quota_attempts']::pg_catalog.name[])
        HAVING pg_catalog.count(*) OPERATOR(pg_catalog.=) 3::pg_catalog.int8
           AND pg_catalog.count(DISTINCT c.relowner) OPERATOR(pg_catalog.=) 1::pg_catalog.int8);
BEGIN
    IF pg_catalog.current_database() OPERATOR(pg_catalog.<>) 'defaultdb'::pg_catalog.name
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER
       OR (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER)
       OR SESSION_USER OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner', 'fcb_app', 'fcb_signer']::pg_catalog.name[])
       OR tables_owner IS NULL
       OR tables_owner OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner', 'fcb_app', 'fcb_signer']::pg_catalog.name[]) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_QUOTA_SCHEMA_GUARD_FAILED';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname OPERATOR(pg_catalog.=) 'fcb_cert_quota'::pg_catalog.name) THEN
        EXECUTE pg_catalog.format('CREATE SCHEMA fcb_cert_quota AUTHORIZATION %I', tables_owner);
    END IF;
    IF (SELECT n.nspowner FROM pg_catalog.pg_namespace n WHERE n.nspname OPERATOR(pg_catalog.=) 'fcb_cert_quota'::pg_catalog.name)
       IS DISTINCT FROM (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) tables_owner) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_QUOTA_SCHEMA_OWNER_UNEXPECTED';
    END IF;
END $fcbcertquotaschema$;
