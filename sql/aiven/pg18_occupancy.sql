-- Read-only occupancy evidence for fcb_pg18_test (reviews of #178). Output (psql -A -t -F '='):
-- one `key=count` line per category, in this fixed order. Every catalog relation, function AND
-- operator is schema-qualified (OPERATOR(pg_catalog.x), literals cast to the exact type), and the
-- workflow forces search_path at connect time: objects or operators planted in public cannot
-- shadow anything here (CVE-2018-1058 pattern). Temporary schemas are excluded by an anchored
-- regex (LIKE 'pg_temp%' would also match 'pgxtemp'). Aiven's _avnadmin_monitor is excluded only
-- when a superuser other than the connecting role owns it.
-- The apply step re-runs this same query inside its own transaction (sum must be exactly 0).
WITH me AS (
  SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER
), user_ns AS (
  SELECT n.oid, n.nspname FROM pg_catalog.pg_namespace n
  WHERE n.nspname OPERATOR(pg_catalog.<>) ALL (ARRAY['pg_catalog', 'information_schema', 'pg_toast']::pg_catalog.name[])
    AND n.nspname OPERATOR(pg_catalog.!~) '^pg_(toast_)?temp_[0-9]+$'::pg_catalog.text
    AND NOT (n.nspname OPERATOR(pg_catalog.=) '_avnadmin_monitor'::pg_catalog.name
             AND n.nspowner OPERATOR(pg_catalog.<>) (SELECT me.oid FROM me)
             AND (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.oid OPERATOR(pg_catalog.=) n.nspowner))
)
SELECT k, v FROM (
  SELECT 1 AS o, 'relations' AS k, pg_catalog.count(*) AS v
    FROM pg_catalog.pg_class c JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
  UNION ALL SELECT 2, 'functions', pg_catalog.count(*)
    FROM pg_catalog.pg_proc p JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
  UNION ALL SELECT 3, 'types', pg_catalog.count(*)
    FROM pg_catalog.pg_type t JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) t.typnamespace
    WHERE t.typrelid OPERATOR(pg_catalog.=) 0::pg_catalog.oid
      AND t.typcategory OPERATOR(pg_catalog.<>) 'A'::pg_catalog.char
  UNION ALL SELECT 4, 'other_objects', (
      (SELECT pg_catalog.count(*) FROM pg_catalog.pg_collation x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.collnamespace)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_operator x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.oprnamespace)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_config x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.cfgnamespace)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_dict x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.dictnamespace)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_conversion x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.connamespace)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_extension x WHERE x.extname OPERATOR(pg_catalog.<>) 'plpgsql'::pg_catalog.name)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_largeobject_metadata)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_publication))
  UNION ALL SELECT 5, 'default_privileges', pg_catalog.count(*) FROM pg_catalog.pg_default_acl
  UNION ALL SELECT 6, 'database_settings', pg_catalog.count(*)
    FROM pg_catalog.pg_db_role_setting s, pg_catalog.unnest(s.setconfig) AS c(cfg)
    WHERE (s.setdatabase OPERATOR(pg_catalog.=) (SELECT d.oid FROM pg_catalog.pg_database d
                                               WHERE d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database())
           OR (s.setdatabase OPERATOR(pg_catalog.=) 0::pg_catalog.oid
               AND s.setrole OPERATOR(pg_catalog.=) (SELECT me.oid FROM me)))
      AND c.cfg OPERATOR(pg_catalog.!~) '^search_path='::pg_catalog.text
  UNION ALL SELECT 7, 'unexpected_schemas', pg_catalog.count(*)
    FROM user_ns n WHERE n.nspname OPERATOR(pg_catalog.<>) 'public'::pg_catalog.name
) s ORDER BY o;
