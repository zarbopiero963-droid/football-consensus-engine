-- FCB-WP-005: read-only catalog fingerprint of the CURRENT schema after the restore.
-- One line per object property, schema name stripped, sorted; md5 of the whole text is
-- compared inside the restore transaction with the value pinned in the workflow (PG18),
-- so any tampered or extra object (functions, triggers and their enabled state, privileges,
-- columns, constraints, indexes, rules/views, policies, types) aborts before commit.
-- Data rows are never read; schema_migrations contents are verified separately.
WITH s AS (SELECT current_schema() AS n),
c AS (SELECT cl.* FROM pg_class cl JOIN pg_namespace n ON n.oid = cl.relnamespace, s WHERE n.nspname = s.n),
lines(x) AS (
    SELECT 'rel ' || relname || ' ' || relkind::text || ' ' || relpersistence::text || ' acl=' || COALESCE(relacl::text, '-')
        || ' opt=' || COALESCE(reloptions::text, '-') || ' rls=' || relrowsecurity || ' ri=' || relreplident::text
        || ' own=' || (relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user))
    FROM c WHERE relkind IN ('r', 'p', 'v', 'm', 'S', 'f', 'i', 'I', 'c')
    UNION ALL
    SELECT 'col ' || c.relname || '.' || a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
        || ' coll=' || COALESCE((SELECT cn.nspname || '.' || co.collname FROM pg_collation co
                                 JOIN pg_namespace cn ON cn.oid = co.collnamespace WHERE co.oid = a.attcollation), '-')
        || ' nn=' || a.attnotnull || ' def=' || COALESCE(pg_get_expr(d.adbin, d.adrelid), '-')
        || ' acl=' || COALESCE(a.attacl::text, '-') || ' id=' || a.attidentity::text || ' gen=' || a.attgenerated::text
    FROM c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
    UNION ALL
    SELECT 'con ' || c.relname || ' ' || k.conname || ' ' || k.contype::text || ' ' || pg_get_constraintdef(k.oid)
        || ' valid=' || k.convalidated
    FROM c JOIN pg_constraint k ON k.conrelid = c.oid
    UNION ALL
    SELECT 'idx ' || pg_get_indexdef(i.indexrelid) || ' valid=' || i.indisvalid || ' ready=' || i.indisready
    FROM c JOIN pg_index i ON i.indrelid = c.oid
    UNION ALL
    SELECT 'trg ' || pg_get_triggerdef(t.oid) || ' enabled=' || t.tgenabled::text
    FROM c JOIN pg_trigger t ON t.tgrelid = c.oid WHERE NOT t.tgisinternal
    UNION ALL
    SELECT 'rul ' || c.relname || ' ' || r.rulename || ' ' || pg_get_ruledef(r.oid) || ' enabled=' || r.ev_enabled::text
    FROM c JOIN pg_rewrite r ON r.ev_class = c.oid
    UNION ALL
    SELECT 'pol ' || c.relname || ' ' || p.polname || ' ' || p.polcmd::text || ' ' || COALESCE(pg_get_expr(p.polqual, p.polrelid), '-')
    FROM c JOIN pg_policy p ON p.polrelid = c.oid
    UNION ALL
    SELECT 'fun ' || pg_get_functiondef(p.oid) || ' acl=' || COALESCE(p.proacl::text, '-')
        || ' own=' || (p.proowner = (SELECT oid FROM pg_roles WHERE rolname = current_user))
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, s WHERE n.nspname = s.n AND p.prokind IN ('f', 'p')
    UNION ALL
    SELECT 'typ ' || t.typname || ' ' || t.typtype::text
    FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace, s
    WHERE n.nspname = s.n AND t.typtype IN ('e', 'd', 'r', 'm', 'b')
)
SELECT md5(string_agg(replace(x, (SELECT n FROM s) || '.', ''), E'\n' ORDER BY replace(x, (SELECT n FROM s) || '.', '')))
FROM lines;
