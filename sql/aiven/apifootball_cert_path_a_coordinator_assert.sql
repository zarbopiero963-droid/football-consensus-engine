-- PATH A2, database `defaultdb`, read-only (catalog only): the runner holds EXACTLY the 9
-- quota-coordinator privileges (8 table-level + 1 column-level on the three api_football_quota_*
-- tables), none WITH GRANT OPTION, nothing on any other relation of public, no CREATE on public, and
-- exactly one explicit schema entry: USAGE on public without grant option. Run after the owner's
-- grant step (apifootball_cert_path_a_coordinator_grants.sql, same transaction) and after every
-- runner password rotation (apifootball_cert_path_a_roles.sql alone: no re-grant). Any difference
-- raises FCB_CERT_PATH_A_COORDINATOR_GRANTS_UNEXPECTED.
DO $fcbcertcoordassert$
DECLARE
    runner pg_catalog.oid := (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name);
    got pg_catalog.text;
BEGIN
    IF pg_catalog.current_database() OPERATOR(pg_catalog.<>) 'defaultdb'::pg_catalog.name OR runner IS NULL THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_COORDINATOR_GRANTS_UNEXPECTED';
    END IF;
    SELECT pg_catalog.string_agg(z.e, ',' ORDER BY z.e COLLATE "C") INTO got FROM (
        SELECT c.relname::pg_catalog.text OPERATOR(pg_catalog.||) ':'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text
               OPERATOR(pg_catalog.||) ':'::pg_catalog.text OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e
        FROM pg_catalog.pg_class c, LATERAL pg_catalog.aclexplode(c.relacl) x
        WHERE c.relnamespace OPERATOR(pg_catalog.=) 'public'::pg_catalog.regnamespace AND x.grantee OPERATOR(pg_catalog.=) runner
        UNION ALL
        SELECT c.relname::pg_catalog.text OPERATOR(pg_catalog.||) '.'::pg_catalog.text OPERATOR(pg_catalog.||) a.attname::pg_catalog.text
               OPERATOR(pg_catalog.||) ':'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text
               OPERATOR(pg_catalog.||) ':'::pg_catalog.text OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) c.oid,
             LATERAL pg_catalog.aclexplode(a.attacl) x
        WHERE c.relnamespace OPERATOR(pg_catalog.=) 'public'::pg_catalog.regnamespace AND x.grantee OPERATOR(pg_catalog.=) runner
        UNION ALL
        SELECT 'schema:'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) ':'::pg_catalog.text
               OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text
        FROM pg_catalog.pg_namespace n, LATERAL pg_catalog.aclexplode(n.nspacl) x
        WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.grantee OPERATOR(pg_catalog.=) runner) z;
    IF got IS DISTINCT FROM ('api_football_quota_attempts:DELETE:false,api_football_quota_attempts:INSERT:false,'
           'api_football_quota_attempts:SELECT:false,api_football_quota_counters.used:UPDATE:false,'
           'api_football_quota_counters:INSERT:false,api_football_quota_counters:SELECT:false,'
           'api_football_quota_leases:DELETE:false,api_football_quota_leases:INSERT:false,'
           'api_football_quota_leases:SELECT:false,schema:USAGE:false')::pg_catalog.text
       OR pg_catalog.has_schema_privilege(runner, 'public', 'CREATE') THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_COORDINATOR_GRANTS_UNEXPECTED';
    END IF;
END $fcbcertcoordassert$;
