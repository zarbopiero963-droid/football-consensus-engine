-- PATH A2 variant R1, database `defaultdb`, read-only (catalog only): the end state of step 1b. Run in the
-- SAME transaction after apifootball_cert_path_a_quota_functions.sql (table owner) and after every runner
-- password rotation (apifootball_cert_path_a_roles.sql alone, administrator: no re-definition).
-- Requires:
--   * apifootball_cert_quota_state.sql = EXACT (embedded below byte-for-byte, comments stripped;
--     tests/test_apifootball_cert_path_a.py keeps the copy identical): the four pinned SECURITY DEFINER
--     functions, their exact EXECUTE ACL, the exact schema ACL, nothing else in fcb_cert_quota;
--   * fcb_cert_runner holds NO privilege (table, column or sequence; any privilege type, with or without
--     grant option) on ANY relation of public: no direct SQL on the shared quota tables or anything else;
--   * on schema public NO explicit entry for the runner (owner ratification FCB-A2-R1-WP009-DECISIONS:
--     the SECURITY DEFINER functions need no USAGE on public; WP-009 phase 1 revoked PUBLIC's) and no
--     CREATE.
-- Any difference raises FCB_CERT_PATH_A_QUOTA_STATE_UNEXPECTED.
DO $fcbcertquotaassert$
DECLARE
    runner pg_catalog.oid := (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name);
BEGIN
    IF pg_catalog.current_database() OPERATOR(pg_catalog.<>) 'defaultdb'::pg_catalog.name OR runner IS NULL
       OR (
SELECT CASE
    WHEN NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname OPERATOR(pg_catalog.=) 'fcb_cert_quota'::pg_catalog.name)
        THEN 'ABSENT'::pg_catalog.text
    WHEN COALESCE((
        WITH w AS (
            SELECT r.oid FROM pg_catalog.pg_roles r
            WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name
              AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb AND NOT r.rolbypassrls
              AND NOT r.rolreplication AND NOT r.rolinherit
              AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member OPERATOR(pg_catalog.=) r.oid)
              AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.roleid OPERATOR(pg_catalog.=) r.oid
                              AND NOT (m.admin_option AND NOT m.inherit_option AND NOT m.set_option
                                       AND m.grantor OPERATOR(pg_catalog.=) 10::pg_catalog.oid))
              AND (SELECT pg_catalog.count(*) FROM pg_catalog.pg_auth_members m WHERE m.roleid OPERATOR(pg_catalog.=) r.oid)
                  OPERATOR(pg_catalog.<=) 1::pg_catalog.int8),
        qt AS (
            SELECT c.oid, c.relowner, c.relacl FROM pg_catalog.pg_class c
            WHERE c.relnamespace OPERATOR(pg_catalog.=) 'public'::pg_catalog.regnamespace
              AND c.relkind OPERATOR(pg_catalog.=) 'r'::pg_catalog."char"
              AND c.relname OPERATOR(pg_catalog.=) ANY (ARRAY['api_football_quota_counters', 'api_football_quota_leases',
                                                              'api_football_quota_attempts']::pg_catalog.name[])),
        t AS (SELECT pg_catalog.min(qt.relowner::pg_catalog.text)::pg_catalog.oid AS oid FROM qt
              HAVING pg_catalog.count(*) OPERATOR(pg_catalog.=) 3::pg_catalog.int8
                 AND pg_catalog.count(DISTINCT qt.relowner) OPERATOR(pg_catalog.=) 1::pg_catalog.int8),
        q AS (SELECT n.oid, n.nspowner, n.nspacl FROM pg_catalog.pg_namespace n
              WHERE n.nspname OPERATOR(pg_catalog.=) 'fcb_cert_quota'::pg_catalog.name),
        f AS (SELECT p.* FROM pg_catalog.pg_proc p, q WHERE p.pronamespace OPERATOR(pg_catalog.=) q.oid)
        SELECT (SELECT pg_catalog.count(*) FROM w) OPERATOR(pg_catalog.=) 1::pg_catalog.int8
           AND (SELECT pg_catalog.count(*) FROM t) OPERATOR(pg_catalog.=) 1::pg_catalog.int8
           AND (SELECT q.nspowner FROM q) OPERATOR(pg_catalog.=) (SELECT t.oid FROM t)
           AND (SELECT pg_catalog.string_agg(z.e, ','::pg_catalog.text ORDER BY z.e) FROM (
                    SELECT x.grantor::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text
                       OPERATOR(pg_catalog.||) x.grantee::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text
                       OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text
                       OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e
                    FROM q, LATERAL pg_catalog.aclexplode(q.nspacl) x) z)
               OPERATOR(pg_catalog.=)
               (SELECT pg_catalog.string_agg(e.v, ','::pg_catalog.text ORDER BY e.v) FROM (
                    SELECT t.oid::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) t.oid::pg_catalog.text
                           OPERATOR(pg_catalog.||) '/CREATE/false'::pg_catalog.text AS v FROM t
                    UNION ALL SELECT t.oid::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) t.oid::pg_catalog.text
                           OPERATOR(pg_catalog.||) '/USAGE/false'::pg_catalog.text FROM t
                    UNION ALL SELECT t.oid::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) w.oid::pg_catalog.text
                           OPERATOR(pg_catalog.||) '/USAGE/false'::pg_catalog.text FROM t, w) e)
           AND (SELECT pg_catalog.string_agg(f.proname::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                       OPERATOR(pg_catalog.||) pg_catalog.pg_get_function_arguments(f.oid) OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                       OPERATOR(pg_catalog.||) pg_catalog.pg_get_function_result(f.oid) OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                       OPERATOR(pg_catalog.||) l.lanname::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                       OPERATOR(pg_catalog.||) f.prokind::pg_catalog.text OPERATOR(pg_catalog.||) f.prosecdef::pg_catalog.text
                       OPERATOR(pg_catalog.||) f.provolatile::pg_catalog.text OPERATOR(pg_catalog.||) f.proisstrict::pg_catalog.text
                       OPERATOR(pg_catalog.||) f.proleakproof::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                       OPERATOR(pg_catalog.||) COALESCE(f.proconfig::pg_catalog.text, '-'::pg_catalog.text) OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                       OPERATOR(pg_catalog.||) pg_catalog.md5(f.prosrc) OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                       OPERATOR(pg_catalog.||) (f.proowner OPERATOR(pg_catalog.=) (SELECT t.oid FROM t))::pg_catalog.text
                       OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                       OPERATOR(pg_catalog.||) COALESCE((SELECT pg_catalog.string_agg(z.e, ','::pg_catalog.text ORDER BY z.e) FROM (
                                SELECT x.grantor::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text
                                OPERATOR(pg_catalog.||) x.grantee::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text
                                OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text
                                OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e
                            FROM pg_catalog.aclexplode(f.proacl) x) z)
                          OPERATOR(pg_catalog.=)
                          (SELECT pg_catalog.string_agg(e.v, ','::pg_catalog.text ORDER BY e.v) FROM (
                               SELECT t.oid::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) t.oid::pg_catalog.text
                                      OPERATOR(pg_catalog.||) '/EXECUTE/false'::pg_catalog.text AS v FROM t
                               UNION ALL SELECT t.oid::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) w.oid::pg_catalog.text
                                      OPERATOR(pg_catalog.||) '/EXECUTE/false'::pg_catalog.text FROM t, w) e), false)::pg_catalog.text,
                       ';'::pg_catalog.text ORDER BY f.proname COLLATE "C")
                FROM f JOIN pg_catalog.pg_language l ON l.oid OPERATOR(pg_catalog.=) f.prolang)
               OPERATOR(pg_catalog.=) (
                'mark_exhausted|p_key_hash text, p_kind text|void|plpgsql|ftruevfalsefalse|{"search_path=pg_catalog, pg_temp"}|a324fbae0bb6e11a5ef72a112fde1414|true|true;' OPERATOR(pg_catalog.||)
                'release|p_key_hash text, p_lease_id text|void|plpgsql|ftruevfalsefalse|{"search_path=pg_catalog, pg_temp"}|3559bfb7badfed16609b58384a44ff96|true|true;' OPERATOR(pg_catalog.||)
                'reserve|p_key_hash text|text|plpgsql|ftruevfalsefalse|{"search_path=pg_catalog, pg_temp"}|fcf76fe21351fb67f000845642d740ea|true|true;' OPERATOR(pg_catalog.||)
                'snapshot|p_key_hash text|TABLE(remaining_minute integer, remaining_day integer, active_leases integer)|plpgsql|ftruevfalsefalse|{"search_path=pg_catalog, pg_temp"}|d50a603c2e2ba8ebfa5f9c7b3f369b3e|true|true')::pg_catalog.text
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class x, q WHERE x.relnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_type x, q WHERE x.typnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_operator x, q WHERE x.oprnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_opclass x, q WHERE x.opcnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_opfamily x, q WHERE x.opfnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_collation x, q WHERE x.collnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_conversion x, q WHERE x.connamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_ts_config x, q WHERE x.cfgnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_ts_dict x, q WHERE x.dictnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_ts_parser x, q WHERE x.prsnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_ts_template x, q WHERE x.tmplnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_extension x, q WHERE x.extnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_statistic_ext x, q WHERE x.stxnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint x, q WHERE x.connamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_default_acl x, q WHERE x.defaclnamespace OPERATOR(pg_catalog.=) q.oid)
           AND NOT EXISTS (SELECT 1 FROM qt, LATERAL pg_catalog.aclexplode(qt.relacl) x
                           WHERE x.grantee OPERATOR(pg_catalog.=) ANY (ARRAY[0::pg_catalog.oid, (SELECT w.oid FROM w)]))
           AND NOT EXISTS (SELECT 1 FROM qt JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) qt.oid,
                           LATERAL pg_catalog.aclexplode(a.attacl) x
                           WHERE x.grantee OPERATOR(pg_catalog.=) ANY (ARRAY[0::pg_catalog.oid, (SELECT w.oid FROM w)]))
           AND NOT pg_catalog.has_schema_privilege((SELECT w.oid FROM w), 'public'::pg_catalog.text, 'CREATE'::pg_catalog.text)
        ), false)
        THEN 'EXACT'::pg_catalog.text
    ELSE 'DRIFT'::pg_catalog.text
END AS state
) IS DISTINCT FROM 'EXACT'::pg_catalog.text
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c, LATERAL pg_catalog.aclexplode(c.relacl) x
                  WHERE c.relnamespace OPERATOR(pg_catalog.=) 'public'::pg_catalog.regnamespace
                    AND x.grantee OPERATOR(pg_catalog.=) runner)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) c.oid,
                  LATERAL pg_catalog.aclexplode(a.attacl) x
                  WHERE c.relnamespace OPERATOR(pg_catalog.=) 'public'::pg_catalog.regnamespace
                    AND x.grantee OPERATOR(pg_catalog.=) runner)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n, LATERAL pg_catalog.aclexplode(n.nspacl) x
                  WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.grantee OPERATOR(pg_catalog.=) runner)
       OR pg_catalog.has_schema_privilege(runner, 'public', 'CREATE') THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_QUOTA_STATE_UNEXPECTED';
    END IF;
END $fcbcertquotaassert$;
