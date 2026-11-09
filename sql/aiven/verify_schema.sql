-- Read-only schema evidence for the 0001-0007 apply (schema public). No data is read.
-- Explicit ordinal so the output order is guaranteed (workflow reads the first 7 rows as counts).
-- Every catalog reference is schema-qualified (review of #178, P2: no shadowing via search_path).
SELECT k, v FROM (
  SELECT 1 AS o, 'tables' AS k, pg_catalog.count(*) AS v FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p')
  UNION ALL SELECT 2, 'functions', pg_catalog.count(*) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.prokind='f'
  UNION ALL SELECT 3, 'triggers', pg_catalog.count(*) FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
    JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal
  UNION ALL SELECT 4, 'indexes', pg_catalog.count(*) FROM pg_catalog.pg_indexes WHERE schemaname='public'
  UNION ALL SELECT 5, 'constraints_information_schema', pg_catalog.count(*) FROM information_schema.table_constraints
    WHERE constraint_schema='public'
  UNION ALL SELECT 6, 'constraints_pg_constraint', pg_catalog.count(*) FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_namespace n ON n.oid=k.connamespace
    WHERE n.nspname='public' AND k.contype IN ('p','u','f','c','x')
  UNION ALL SELECT 7, 'schema_migrations_rows', pg_catalog.count(*) FROM public.schema_migrations
) s ORDER BY o;
SELECT version, name, sha256 FROM public.schema_migrations ORDER BY version;
