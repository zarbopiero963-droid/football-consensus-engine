-- FCB-WP-005: run INSIDE the restore transaction, after the bridge script. Aborts the whole
-- transaction (nothing committed) unless the object counts equal :'expected_counts' (psql -v).
-- Same counts and order as verify_bridge_schema.sql. No data row is read.
SELECT set_config('fcb.expected_counts', :'expected_counts', true) IS NOT NULL AS expected_set;
DO $fcbcounts$
DECLARE got text;
BEGIN
    SELECT string_agg(k || '=' || v, ' ' ORDER BY o) INTO got FROM (
      SELECT 1 AS o, 'tables' AS k, count(*) AS v FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema() AND c.relkind IN ('r','p')
      UNION ALL SELECT 2, 'functions', count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname=current_schema() AND p.prokind='f'
      UNION ALL SELECT 3, 'triggers', count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() AND NOT t.tgisinternal
      UNION ALL SELECT 4, 'indexes', count(*) FROM pg_indexes WHERE schemaname=current_schema()
      UNION ALL SELECT 5, 'constraints_information_schema', count(*) FROM information_schema.table_constraints
        WHERE constraint_schema=current_schema()
      UNION ALL SELECT 6, 'constraints_pg_constraint', count(*) FROM pg_constraint k JOIN pg_namespace n ON n.oid=k.connamespace
        WHERE n.nspname=current_schema() AND k.contype IN ('p','u','f','c','x')
      UNION ALL SELECT 7, 'schema_migrations_rows', count(*) FROM schema_migrations
    ) s;
    IF got IS DISTINCT FROM current_setting('fcb.expected_counts') THEN
        RAISE EXCEPTION 'FCB_RESTORE_COUNTS_MISMATCH %', got;
    END IF;
END $fcbcounts$;
