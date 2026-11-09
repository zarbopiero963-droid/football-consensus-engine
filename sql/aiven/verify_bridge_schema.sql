-- FCB-WP-005: read-only schema evidence for the bridge 0001-0008 restore (schema public). No data is read.
-- Explicit ordinal so the output order is guaranteed (workflow reads the first 7 rows as counts).
-- Every catalog relation, function and operator is schema-qualified (audit F3).
SELECT s.k, s.v FROM (
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
) s ORDER BY s.o;
SELECT m.version, m.name, m.sha256 FROM public.schema_migrations m ORDER BY m.version;
