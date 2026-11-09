-- FCB-WP-005: run INSIDE the restore transaction, after the bridge script. Aborts the whole
-- transaction (nothing committed) unless the object counts equal :'expected_counts' (psql -v).
-- Same counts and order as verify_bridge_schema.sql. No data row is read.
-- Audit F3: code reached during the script could have changed search_path until COMMIT. Such a
-- change aborts here (checked with qualified names only), then the path is pinned again, and
-- every catalog relation, function and operator below is schema-qualified anyway.
DO $fcbpath$
BEGIN
    IF pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) 'public'::pg_catalog.text
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER THEN
        RAISE EXCEPTION 'FCB_RESTORE_SESSION_GUARD_FAILED';
    END IF;
END $fcbpath$;
SET LOCAL search_path = public;
SELECT pg_catalog.set_config('fcb.expected_counts', :'expected_counts', true) IS NOT NULL AS expected_set;
DO $fcbcounts$
DECLARE got pg_catalog.text;
BEGIN
    SELECT pg_catalog.string_agg(s.k OPERATOR(pg_catalog.||) '='::pg_catalog.text OPERATOR(pg_catalog.||) s.v::pg_catalog.text,
                                 ' '::pg_catalog.text ORDER BY s.o) INTO got FROM (
  SELECT 1 AS o, 'tables'::pg_catalog.text AS k, pg_catalog.count(*) AS v FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
    WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p']::pg_catalog."char"[])
  UNION ALL SELECT 2, 'functions'::pg_catalog.text, pg_catalog.count(*) FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
    WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND p.prokind OPERATOR(pg_catalog.=) 'f'::pg_catalog."char"
  UNION ALL SELECT 3, 'triggers'::pg_catalog.text, pg_catalog.count(*) FROM pg_catalog.pg_trigger t
    JOIN pg_catalog.pg_class c ON c.oid OPERATOR(pg_catalog.=) t.tgrelid
    JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
    WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND NOT t.tgisinternal
  UNION ALL SELECT 4, 'indexes'::pg_catalog.text, pg_catalog.count(*) FROM pg_catalog.pg_indexes x
    WHERE x.schemaname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
  UNION ALL SELECT 5, 'constraints_information_schema'::pg_catalog.text, pg_catalog.count(*) FROM information_schema.table_constraints x
    WHERE x.constraint_schema::pg_catalog.name OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
  UNION ALL SELECT 6, 'constraints_pg_constraint'::pg_catalog.text, pg_catalog.count(*) FROM pg_catalog.pg_constraint k
    JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) k.connamespace
    WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
      AND k.contype OPERATOR(pg_catalog.=) ANY (ARRAY['p', 'u', 'f', 'c', 'x']::pg_catalog."char"[])
  UNION ALL SELECT 7, 'schema_migrations_rows'::pg_catalog.text, pg_catalog.count(*) FROM public.schema_migrations
    ) s;
    IF got IS NULL OR got OPERATOR(pg_catalog.<>) pg_catalog.current_setting('fcb.expected_counts') THEN
        RAISE EXCEPTION 'FCB_RESTORE_COUNTS_MISMATCH %', got;
    END IF;
END $fcbcounts$;
