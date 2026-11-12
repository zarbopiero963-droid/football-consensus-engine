-- PATH A2 (owner GO): the ONLY accepted ACL besides none is the fcb_cert_runner coordinator grant.
-- FCB-WP-005: read-only catalog fingerprint of the schema public after the restore.
-- One line per object property, schema name stripped, sorted; md5 of the whole text is
-- compared inside the restore transaction with the value pinned in the workflow (PG18),
-- so any tampered or extra object (functions, triggers and their enabled state, privileges,
-- columns, constraints, indexes, rules/views, policies, types) aborts before commit.
-- Every catalog relation, function and operator is schema-qualified (audit F3): the output is
-- the same as before the qualification, and objects planted in public cannot change it.
-- Data rows are never read; schema_migrations contents are verified separately.
WITH s AS (SELECT 'public'::pg_catalog.text AS n),
me AS (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) CURRENT_USER),
c AS (SELECT cl.* FROM pg_catalog.pg_class cl JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) cl.relnamespace, s
      WHERE n.nspname OPERATOR(pg_catalog.=) s.n::pg_catalog.name),
lines(x) AS (
    SELECT 'rel '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) c.relkind::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) c.relpersistence::pg_catalog.text OPERATOR(pg_catalog.||) ' acl='::pg_catalog.text
        OPERATOR(pg_catalog.||) CASE WHEN (c.relacl IS NULL OR (c.relname::pg_catalog.text OPERATOR(pg_catalog.=) ANY (ARRAY['api_football_quota_counters', 'api_football_quota_leases', 'api_football_quota_attempts']::pg_catalog.text[]) AND (SELECT pg_catalog.string_agg(z.e, ','::pg_catalog.text ORDER BY z.e) FROM (SELECT x.grantor::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.grantee::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e FROM pg_catalog.aclexplode(c.relacl) x) z) OPERATOR(pg_catalog.=) (SELECT pg_catalog.string_agg(z.e, ','::pg_catalog.text ORDER BY z.e) FROM (SELECT x.grantor::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.grantee::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e FROM pg_catalog.aclexplode(pg_catalog.acldefault('r'::pg_catalog."char", c.relowner)) x UNION ALL SELECT c.relowner::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) w.oid::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) pv.pv OPERATOR(pg_catalog.||) '/false'::pg_catalog.text FROM (SELECT r.oid AS oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb AND NOT r.rolbypassrls AND NOT r.rolreplication AND NOT r.rolinherit AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member OPERATOR(pg_catalog.=) r.oid)) w, pg_catalog.unnest(CASE WHEN c.relname::pg_catalog.text OPERATOR(pg_catalog.=) 'api_football_quota_counters'::pg_catalog.text THEN ARRAY['INSERT', 'SELECT']::pg_catalog.text[] ELSE ARRAY['DELETE', 'INSERT', 'SELECT']::pg_catalog.text[] END) pv) z))) THEN '-'::pg_catalog.text ELSE c.relacl::pg_catalog.text END
        OPERATOR(pg_catalog.||) ' opt='::pg_catalog.text OPERATOR(pg_catalog.||) COALESCE(c.reloptions::pg_catalog.text, '-'::pg_catalog.text)
        OPERATOR(pg_catalog.||) ' rls='::pg_catalog.text OPERATOR(pg_catalog.||) c.relrowsecurity::pg_catalog.text
        OPERATOR(pg_catalog.||) ' ri='::pg_catalog.text OPERATOR(pg_catalog.||) c.relreplident::pg_catalog.text
        OPERATOR(pg_catalog.||) ' own='::pg_catalog.text
        OPERATOR(pg_catalog.||) (c.relowner OPERATOR(pg_catalog.=) (SELECT me.oid FROM me))::pg_catalog.text
    FROM c WHERE c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'v', 'm', 'S', 'f', 'i', 'I', 'c']::pg_catalog."char"[])
    UNION ALL
    SELECT 'col '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) '.'::pg_catalog.text
        OPERATOR(pg_catalog.||) a.attname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) pg_catalog.format_type(a.atttypid, a.atttypmod)
        OPERATOR(pg_catalog.||) ' coll='::pg_catalog.text
        OPERATOR(pg_catalog.||) COALESCE((SELECT cn.nspname::pg_catalog.text OPERATOR(pg_catalog.||) '.'::pg_catalog.text
                                                 OPERATOR(pg_catalog.||) co.collname::pg_catalog.text
                                          FROM pg_catalog.pg_collation co
                                          JOIN pg_catalog.pg_namespace cn ON cn.oid OPERATOR(pg_catalog.=) co.collnamespace
                                          WHERE co.oid OPERATOR(pg_catalog.=) a.attcollation), '-'::pg_catalog.text)
        OPERATOR(pg_catalog.||) ' nn='::pg_catalog.text OPERATOR(pg_catalog.||) a.attnotnull::pg_catalog.text
        OPERATOR(pg_catalog.||) ' def='::pg_catalog.text
        OPERATOR(pg_catalog.||) COALESCE(pg_catalog.pg_get_expr(d.adbin, d.adrelid), '-'::pg_catalog.text)
        OPERATOR(pg_catalog.||) ' acl='::pg_catalog.text OPERATOR(pg_catalog.||) CASE WHEN (a.attacl IS NULL OR (c.relname::pg_catalog.text OPERATOR(pg_catalog.=) 'api_football_quota_counters'::pg_catalog.text AND a.attname OPERATOR(pg_catalog.=) 'used'::pg_catalog.name AND (SELECT pg_catalog.string_agg(z.e, ','::pg_catalog.text ORDER BY z.e) FROM (SELECT x.grantor::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.grantee::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e FROM pg_catalog.aclexplode(a.attacl) x) z) OPERATOR(pg_catalog.=) (SELECT (SELECT o.relowner FROM pg_catalog.pg_class o WHERE o.oid OPERATOR(pg_catalog.=) a.attrelid)::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) w.oid::pg_catalog.text OPERATOR(pg_catalog.||) '/UPDATE/false'::pg_catalog.text FROM (SELECT r.oid AS oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb AND NOT r.rolbypassrls AND NOT r.rolreplication AND NOT r.rolinherit AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member OPERATOR(pg_catalog.=) r.oid)) w))) THEN '-'::pg_catalog.text ELSE a.attacl::pg_catalog.text END
        OPERATOR(pg_catalog.||) ' id='::pg_catalog.text OPERATOR(pg_catalog.||) a.attidentity::pg_catalog.text
        OPERATOR(pg_catalog.||) ' gen='::pg_catalog.text OPERATOR(pg_catalog.||) a.attgenerated::pg_catalog.text
    FROM c JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) c.oid
        AND a.attnum OPERATOR(pg_catalog.>) 0::pg_catalog.int2 AND NOT a.attisdropped
    LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid OPERATOR(pg_catalog.=) c.oid AND d.adnum OPERATOR(pg_catalog.=) a.attnum
    WHERE c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'v', 'm', 'f']::pg_catalog."char"[])
    UNION ALL
    SELECT 'con '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) k.conname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) k.contype::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) pg_catalog.pg_get_constraintdef(k.oid)
        OPERATOR(pg_catalog.||) ' valid='::pg_catalog.text OPERATOR(pg_catalog.||) k.convalidated::pg_catalog.text
    FROM c JOIN pg_catalog.pg_constraint k ON k.conrelid OPERATOR(pg_catalog.=) c.oid
    UNION ALL
    SELECT 'idx '::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.pg_get_indexdef(i.indexrelid)
        OPERATOR(pg_catalog.||) ' valid='::pg_catalog.text OPERATOR(pg_catalog.||) i.indisvalid::pg_catalog.text
        OPERATOR(pg_catalog.||) ' ready='::pg_catalog.text OPERATOR(pg_catalog.||) i.indisready::pg_catalog.text
    FROM c JOIN pg_catalog.pg_index i ON i.indrelid OPERATOR(pg_catalog.=) c.oid
    UNION ALL
    SELECT 'trg '::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.pg_get_triggerdef(t.oid)
        OPERATOR(pg_catalog.||) ' enabled='::pg_catalog.text OPERATOR(pg_catalog.||) t.tgenabled::pg_catalog.text
    FROM c JOIN pg_catalog.pg_trigger t ON t.tgrelid OPERATOR(pg_catalog.=) c.oid WHERE NOT t.tgisinternal
    UNION ALL
    SELECT 'rul '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) r.rulename::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) pg_catalog.pg_get_ruledef(r.oid)
        OPERATOR(pg_catalog.||) ' enabled='::pg_catalog.text OPERATOR(pg_catalog.||) r.ev_enabled::pg_catalog.text
    FROM c JOIN pg_catalog.pg_rewrite r ON r.ev_class OPERATOR(pg_catalog.=) c.oid
    UNION ALL
    SELECT 'pol '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) p.polname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) p.polcmd::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) COALESCE(pg_catalog.pg_get_expr(p.polqual, p.polrelid), '-'::pg_catalog.text)
    FROM c JOIN pg_catalog.pg_policy p ON p.polrelid OPERATOR(pg_catalog.=) c.oid
    UNION ALL
    SELECT 'fun '::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.pg_get_functiondef(p.oid)
        OPERATOR(pg_catalog.||) ' acl='::pg_catalog.text OPERATOR(pg_catalog.||) COALESCE(p.proacl::pg_catalog.text, '-'::pg_catalog.text)
        OPERATOR(pg_catalog.||) ' own='::pg_catalog.text
        OPERATOR(pg_catalog.||) (p.proowner OPERATOR(pg_catalog.=) (SELECT me.oid FROM me))::pg_catalog.text
    FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace, s
    WHERE n.nspname OPERATOR(pg_catalog.=) s.n::pg_catalog.name
      AND p.prokind OPERATOR(pg_catalog.=) ANY (ARRAY['f', 'p']::pg_catalog."char"[])
    UNION ALL
    SELECT 'typ '::pg_catalog.text OPERATOR(pg_catalog.||) t.typname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) t.typtype::pg_catalog.text
    FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) t.typnamespace, s
    WHERE n.nspname OPERATOR(pg_catalog.=) s.n::pg_catalog.name
      AND t.typtype OPERATOR(pg_catalog.=) ANY (ARRAY['e', 'd', 'r', 'm', 'b']::pg_catalog."char"[])
)
SELECT pg_catalog.md5(pg_catalog.string_agg(
    pg_catalog.replace(l.x, (SELECT s.n FROM s) OPERATOR(pg_catalog.||) '.'::pg_catalog.text, ''::pg_catalog.text), E'\n'::pg_catalog.text
    ORDER BY pg_catalog.replace(l.x, (SELECT s.n FROM s) OPERATOR(pg_catalog.||) '.'::pg_catalog.text, ''::pg_catalog.text)))
FROM lines l;
