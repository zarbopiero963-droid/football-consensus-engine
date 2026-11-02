-- Read-only schema evidence for the 0001-0007 apply (schema public). No data is read.
SELECT 'tables', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND c.relkind IN ('r','p')
UNION ALL SELECT 'functions', count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname='public' AND p.prokind='f'
UNION ALL SELECT 'triggers', count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
  JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal
UNION ALL SELECT 'indexes', count(*) FROM pg_indexes WHERE schemaname='public'
UNION ALL SELECT 'constraints_information_schema', count(*) FROM information_schema.table_constraints
  WHERE constraint_schema='public'
UNION ALL SELECT 'constraints_pg_constraint', count(*) FROM pg_constraint k JOIN pg_namespace n ON n.oid=k.connamespace
  WHERE n.nspname='public' AND k.contype IN ('p','u','f','c','x')
UNION ALL SELECT 'schema_migrations_rows', count(*) FROM schema_migrations;
SELECT version, name, sha256 FROM schema_migrations ORDER BY version;
