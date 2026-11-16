-- PATH A2 variant R1: read-only, catalog-only state of the runner's quota functions in `defaultdb`.
-- Prints ABSENT (no schema fcb_cert_quota), EXACT or DRIFT. EXACT requires ALL of:
--   * fcb_cert_runner exists as a PLAIN role (no attribute, member of nothing, nobody can act as it but
--     the automatic ADMIN-only bootstrap grant);
--   * the three api_football_quota_* tables of public have ONE owner T, and T owns fcb_cert_quota;
--   * fcb_cert_quota ACL exactly {T: USAGE+CREATE, fcb_cert_runner: USAGE}, granted by T, no grant option;
--   * fcb_cert_quota contains exactly the four pinned functions (name, arguments, result, plpgsql,
--     SECURITY DEFINER, VOLATILE, not STRICT, search_path=pg_catalog, pg_temp, md5 of the body), each
--     owned by T with EXECUTE exactly {T, fcb_cert_runner} (no PUBLIC), and NO other object of any kind
--     (relation, type, operator, opclass/opfamily, collation, conversion, text search, extension,
--     statistics object, constraint, default privilege);
--   * fcb_cert_runner (or PUBLIC) holds NO privilege on any relation or column of public's three
--     quota tables, and the runner has no CREATE on public.
-- Used by apifootball_cert_path_a_quota_assert.sql (embedded byte-for-byte) and by the campaign
-- runner's pre-flight (scripts/apifootball_cert_batch1.py). Every catalog object is qualified.
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
           -- schema ACL: exactly T USAGE+CREATE and the runner USAGE, granted by T, no grant option
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
           -- exactly the four pinned functions, owned by T, EXECUTE exactly {T, runner} granted by T
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
                'mark_exhausted|p_key_hash text, p_kind text|void|plpgsql|ftruevfalsefalse|{"search_path=pg_catalog, pg_temp"}|a324fbae0bb6e11a5ef72a112fde1414|true|true;'
                'release|p_key_hash text, p_lease_id text|void|plpgsql|ftruevfalsefalse|{"search_path=pg_catalog, pg_temp"}|3559bfb7badfed16609b58384a44ff96|true|true;'
                'reserve|p_key_hash text|text|plpgsql|ftruevfalsefalse|{"search_path=pg_catalog, pg_temp"}|fcf76fe21351fb67f000845642d740ea|true|true;'
                'snapshot|p_key_hash text|TABLE(remaining_minute integer, remaining_day integer, active_leases integer)|plpgsql|ftruevfalsefalse|{"search_path=pg_catalog, pg_temp"}|d50a603c2e2ba8ebfa5f9c7b3f369b3e|true|true')::pg_catalog.text
           -- nothing else in the schema
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
           -- the runner (and PUBLIC) hold nothing on the three shared tables, at table or column level
           AND NOT EXISTS (SELECT 1 FROM qt, LATERAL pg_catalog.aclexplode(qt.relacl) x
                           WHERE x.grantee OPERATOR(pg_catalog.=) ANY (ARRAY[0::pg_catalog.oid, (SELECT w.oid FROM w)]))
           AND NOT EXISTS (SELECT 1 FROM qt JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) qt.oid,
                           LATERAL pg_catalog.aclexplode(a.attacl) x
                           WHERE x.grantee OPERATOR(pg_catalog.=) ANY (ARRAY[0::pg_catalog.oid, (SELECT w.oid FROM w)]))
           AND NOT pg_catalog.has_schema_privilege((SELECT w.oid FROM w), 'public'::pg_catalog.text, 'CREATE'::pg_catalog.text)
        ), false)
        THEN 'EXACT'::pg_catalog.text
    ELSE 'DRIFT'::pg_catalog.text
END AS state;
