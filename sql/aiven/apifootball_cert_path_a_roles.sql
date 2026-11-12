-- PATH A (owner mandate FCB-OWNER-APIFOOTBALL-PATH-A-20261112), step 1 of 3, database `defaultdb`.
-- Run in ONE transaction by the existing administrative identity (Aiven's avnadmin: no superuser,
-- CREATEROLE). No WP-009 phase, no ownership change of any existing object, no change to the
-- Render DATABASE_URL. Creates two DEDICATED roles and grants the runner the minimum it needs on
-- the shared API-FOOTBALL quota coordinator (CLAUDE.md §2.4), nothing else:
--   fcb_cert_owner   NOLOGIN: owns the database fcb_cert and every ledger object (step 2/3)
--   fcb_cert_runner  LOGIN (SCRAM verifier only): the campaign runner's identity
-- Precondition set by the caller IN THIS TRANSACTION: fcb_cert.runner_verifier = the SCRAM-SHA-256
-- verifier of the runner password (computed client-side; the cleartext never reaches the server).
-- Aiven runs with createrole_self_grant = 'inherit,set': it is turned off for this transaction, so
-- the creator keeps only PostgreSQL's automatic ADMIN-only membership (no INHERIT, no SET).
-- Coordinator privileges: EXACTLY these 9 (8 table-level + 1 column-level, in 4 GRANT statements),
-- each required by bridge/api_football/quota.py (PostgresQuota), the only SQL the runner sends to
-- defaultdb (tests/integration/test_apifootball_cert_path_a_postgres.py revokes each one in turn and
-- shows the coordinator then fails):
--   api_football_quota_counters  SELECT       _reserve/_snapshot read `used`; ON CONFLICT ... WHERE used < max RETURNING used
--   api_football_quota_counters  INSERT       _reserve/_mark_exhausted upsert of the minute/day bucket
--   api_football_quota_counters  UPDATE(used) the ON CONFLICT DO UPDATE SET used = ... (+1 or GREATEST); no other column
--   api_football_quota_leases    SELECT       _reserve/_snapshot count active leases; DELETE ... WHERE reads columns
--   api_football_quota_leases    INSERT       _reserve takes the lease
--   api_football_quota_leases    DELETE       _reserve expired-lease cleanup; _release
--   api_football_quota_attempts  SELECT       rolling 60 s window count / retry-after / provider-zero probe
--   api_football_quota_attempts  INSERT       _reserve records the attempt; _mark_exhausted fills the window
--   api_football_quota_attempts  DELETE       _reserve prunes attempts older than 60 s
-- No schema grant: USAGE on public comes from PUBLIC (asserted below). No TRUNCATE, REFERENCES,
-- TRIGGER, MAINTAIN or grant option; nothing on any other table, sequence or function.
-- Catalog-qualified. Idempotent: a second run changes nothing.
SET LOCAL createrole_self_grant = '';
SET LOCAL search_path = pg_catalog, public;
DO $fcbcertguard$
BEGIN
    IF pg_catalog.current_database() OPERATOR(pg_catalog.<>) 'defaultdb'::pg_catalog.name
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER
       OR (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER)
       OR NOT (SELECT r.rolcreaterole FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER)
       OR SESSION_USER OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner']::pg_catalog.name[])
       OR pg_catalog.current_setting('createrole_self_grant') OPERATOR(pg_catalog.<>) ''::pg_catalog.text
       OR pg_catalog.current_setting('fcb_cert.runner_verifier', true) IS NULL
       OR pg_catalog.current_setting('fcb_cert.runner_verifier', true) OPERATOR(pg_catalog.!~)
          '^SCRAM-SHA-256\$4096:[A-Za-z0-9+/]+=*\$[A-Za-z0-9+/]+=*:[A-Za-z0-9+/]+=*$'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_ROLES_GUARD_FAILED';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_owner'::pg_catalog.name) THEN
        CREATE ROLE fcb_cert_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name) THEN
        CREATE ROLE fcb_cert_runner NOLOGIN;
    END IF;
    EXECUTE pg_catalog.format('ALTER ROLE fcb_cert_runner PASSWORD %L',
                              pg_catalog.current_setting('fcb_cert.runner_verifier'));
END $fcbcertguard$;
ALTER ROLE fcb_cert_owner WITH NOLOGIN NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD NULL;
ALTER ROLE fcb_cert_runner WITH LOGIN NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS CONNECTION LIMIT 12;

GRANT SELECT, INSERT ON TABLE public.api_football_quota_counters TO fcb_cert_runner;
GRANT UPDATE (used) ON TABLE public.api_football_quota_counters TO fcb_cert_runner;
GRANT SELECT, INSERT, DELETE ON TABLE public.api_football_quota_leases, public.api_football_quota_attempts TO fcb_cert_runner;

DO $fcbcertassert$
DECLARE
    t pg_catalog.text;
    allowed pg_catalog.text[] := ARRAY['api_football_quota_counters', 'api_football_quota_leases',
                                       'api_football_quota_attempts']::pg_catalog.text[];
BEGIN
    -- the two roles: no attribute above a plain role, no membership in anything, nobody acts as them
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
               WHERE r.rolname OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner']::pg_catalog.name[])
                 AND (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls OR r.rolinherit))
       OR (SELECT r.rolcanlogin FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_owner'::pg_catalog.name)
       OR NOT (SELECT r.rolcanlogin FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid OPERATOR(pg_catalog.=) m.member
                  WHERE r.rolname OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner']::pg_catalog.name[]))
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid OPERATOR(pg_catalog.=) m.roleid
                  WHERE r.rolname OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner']::pg_catalog.name[])
                    AND (m.inherit_option OR m.set_option)) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_ROLES_UNEXPECTED';
    END IF;
    -- the runner: no CREATE in public, and on the 33+ existing tables nothing but the coordinator grant
    IF pg_catalog.has_schema_privilege('fcb_cert_runner', 'public', 'CREATE') THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_RUNNER_CAN_CREATE';
    END IF;
    -- no schema grant is made: USAGE on public comes from PUBLIC (PostgreSQL 15+ default); without
    -- it the runner could not reach the coordinator, so the install refuses instead of widening
    IF NOT pg_catalog.has_schema_privilege('fcb_cert_runner', 'public', 'USAGE')
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n, LATERAL pg_catalog.aclexplode(n.nspacl) a
                  JOIN pg_catalog.pg_roles r ON r.oid OPERATOR(pg_catalog.=) a.grantee
                  WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
                    AND r.rolname OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner']::pg_catalog.name[])) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_RUNNER_SCHEMA_USAGE_UNEXPECTED';
    END IF;
    FOR t IN SELECT c.relname::pg_catalog.text FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
             WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
               AND c.relkind::pg_catalog.text OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'v', 'm', 'f']::pg_catalog.text[]) LOOP
        IF t OPERATOR(pg_catalog.<>) ALL (allowed) THEN
            IF pg_catalog.has_table_privilege('fcb_cert_runner', ('public.' OPERATOR(pg_catalog.||) pg_catalog.quote_ident(t))::pg_catalog.regclass,
                                              'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
               OR pg_catalog.has_any_column_privilege('fcb_cert_runner', ('public.' OPERATOR(pg_catalog.||) pg_catalog.quote_ident(t))::pg_catalog.regclass,
                                                      'SELECT,INSERT,UPDATE,REFERENCES') THEN
                RAISE EXCEPTION 'FCB_CERT_PATH_A_RUNNER_GRANTS_UNEXPECTED';
            END IF;
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
               WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND c.relkind OPERATOR(pg_catalog.=) 'S'::"char"
                 AND CASE WHEN c.relkind OPERATOR(pg_catalog.=) 'S'::"char"
                          THEN pg_catalog.has_sequence_privilege('fcb_cert_runner', c.oid, 'USAGE,SELECT,UPDATE') ELSE false END) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_RUNNER_GRANTS_UNEXPECTED';
    END IF;
    IF pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_quota_counters', 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_quota_leases', 'UPDATE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_quota_attempts', 'UPDATE,TRUNCATE,REFERENCES,TRIGGER')
       OR NOT pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_quota_counters', 'SELECT')
       OR NOT pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_quota_counters', 'INSERT')
       OR NOT pg_catalog.has_column_privilege('fcb_cert_runner', 'public.api_football_quota_counters', 'used', 'UPDATE')
       OR pg_catalog.has_column_privilege('fcb_cert_runner', 'public.api_football_quota_counters', 'api_key_hash', 'UPDATE')
       OR NOT pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_quota_leases', 'SELECT')
       OR NOT pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_quota_leases', 'INSERT')
       OR NOT pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_quota_leases', 'DELETE')
       OR NOT pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_quota_attempts', 'SELECT')
       OR NOT pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_quota_attempts', 'INSERT')
       OR NOT pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_quota_attempts', 'DELETE') THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_COORDINATOR_GRANTS_UNEXPECTED';
    END IF;
    -- every existing table keeps its owner: this file never changes ownership
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
               JOIN pg_catalog.pg_roles o ON o.oid OPERATOR(pg_catalog.=) c.relowner
               WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
                 AND o.rolname OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner']::pg_catalog.name[])) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_DEFAULTDB_OWNERSHIP_CHANGED';
    END IF;
END $fcbcertassert$;
