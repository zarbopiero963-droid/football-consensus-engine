-- FCB-WP-009: catalog-only object counts of schema public, independent of the reader's privileges
-- (information_schema hides objects the reader holds no privilege on, so its constraint count and
-- the schema_migrations row count are taken elsewhere: the rows AS fcb_app by
-- `scripts/fcb_db_roles.py evidence`). Reads no table row; runs no user code. Same predicates and
-- order as sql/aiven/verify_bridge_schema.sql; one line, e.g.
--   tables=33 functions=4 triggers=9 indexes=60 constraints_pg_constraint=65
-- Every catalog relation, function and operator is schema-qualified (audit F3).
SELECT pg_catalog.string_agg(s.k OPERATOR(pg_catalog.||) '='::pg_catalog.text OPERATOR(pg_catalog.||) s.v::pg_catalog.text,
                             ' '::pg_catalog.text ORDER BY s.o) FROM (
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
  UNION ALL SELECT 4, 'indexes'::pg_catalog.text, pg_catalog.count(*) FROM pg_catalog.pg_index i
    JOIN pg_catalog.pg_class c ON c.oid OPERATOR(pg_catalog.=) i.indexrelid
    JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
    WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
  UNION ALL SELECT 6, 'constraints_pg_constraint'::pg_catalog.text, pg_catalog.count(*) FROM pg_catalog.pg_constraint k
    JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) k.connamespace
    WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
      AND k.contype OPERATOR(pg_catalog.=) ANY (ARRAY['p', 'u', 'f', 'c', 'x']::pg_catalog."char"[])
) s;
