-- Read-only occupancy evidence for fcb_pg18_test (reviews of #178). Output (psql -A -t -F '='):
-- one `key=count` line per category, in this fixed order. Every catalog reference is schema-
-- qualified, and the workflow also forces search_path at connect time, so objects named like
-- catalogs in public cannot shadow them. Temporary schemas are excluded by an anchored regex
-- (LIKE 'pg_temp%' would also match e.g. 'pgxtemp', since '_' is a LIKE wildcard). Aiven's
-- _avnadmin_monitor is excluded only when the connecting role does not own it.
-- The apply step re-runs this same query inside its own transaction (sum must be 0).
WITH user_ns AS (
  SELECT n.oid, n.nspname FROM pg_catalog.pg_namespace n
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
    AND n.nspname OPERATOR(pg_catalog.!~) '^pg_(toast_)?temp_[0-9]+$'
    AND NOT (n.nspname = '_avnadmin_monitor'
             AND n.nspowner <> (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname = SESSION_USER))
)
SELECT k, v FROM (
  SELECT 1 AS o, 'relations' AS k, pg_catalog.count(*) AS v
    FROM pg_catalog.pg_class c JOIN user_ns n ON n.oid = c.relnamespace
  UNION ALL SELECT 2, 'functions', pg_catalog.count(*)
    FROM pg_catalog.pg_proc p JOIN user_ns n ON n.oid = p.pronamespace
  UNION ALL SELECT 3, 'types', pg_catalog.count(*)
    FROM pg_catalog.pg_type t JOIN user_ns n ON n.oid = t.typnamespace
    WHERE t.typrelid = 0 AND t.typcategory <> 'A'
  UNION ALL SELECT 4, 'other_objects', (
      (SELECT pg_catalog.count(*) FROM pg_catalog.pg_collation x JOIN user_ns n ON n.oid = x.collnamespace)
    + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_operator x JOIN user_ns n ON n.oid = x.oprnamespace)
    + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_config x JOIN user_ns n ON n.oid = x.cfgnamespace)
    + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_dict x JOIN user_ns n ON n.oid = x.dictnamespace)
    + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_conversion x JOIN user_ns n ON n.oid = x.connamespace)
    + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_extension x WHERE x.extname <> 'plpgsql')
    + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_largeobject_metadata)
    + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_publication))
  UNION ALL SELECT 5, 'default_privileges', pg_catalog.count(*) FROM pg_catalog.pg_default_acl
  UNION ALL SELECT 6, 'database_settings', pg_catalog.count(*)
    FROM pg_catalog.pg_db_role_setting s, pg_catalog.unnest(s.setconfig) AS c(cfg)
    WHERE s.setdatabase = (SELECT d.oid FROM pg_catalog.pg_database d WHERE d.datname = pg_catalog.current_database())
      AND c.cfg OPERATOR(pg_catalog.!~) '^search_path='
  UNION ALL SELECT 7, 'unexpected_schemas', pg_catalog.count(*)
    FROM user_ns n WHERE n.nspname <> 'public'
) s ORDER BY o;
