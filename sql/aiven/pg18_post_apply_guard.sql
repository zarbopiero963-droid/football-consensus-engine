-- Runs in the SAME transaction as the first 0001-0007 apply, AFTER the migration script and
-- before COMMIT (review of #178, round 4, P1-2). Every user object must have been created by THIS
-- transaction: a catalog row whose xmin is another transaction's existed before (and was skipped
-- by IF NOT EXISTS) or was committed concurrently while the apply waited on its advisory lock.
-- Kinds the migrations never create (schemas, extensions, large objects, publications, default
-- ACLs, settings) must not exist at all. Fully qualified (relations, functions and operators).
-- Residual: an object committed by a concurrent session after this statement and before COMMIT.
DO $post$
DECLARE
  me_xid pg_catalog.text := pg_catalog.pg_current_xact_id()::pg_catalog.xid::pg_catalog.text;
  me_role pg_catalog.oid := (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER);
  foreign_rows pg_catalog.int8;
BEGIN
  WITH user_ns AS (
    SELECT n.oid, n.nspname FROM pg_catalog.pg_namespace n
    JOIN pg_catalog.pg_roles r_owner ON r_owner.oid OPERATOR(pg_catalog.=) n.nspowner
    WHERE n.nspname OPERATOR(pg_catalog.<>) ALL (ARRAY['pg_catalog', 'information_schema', 'pg_toast']::pg_catalog.name[])
      AND n.nspname OPERATOR(pg_catalog.!~) '^pg_(toast_)?temp_[0-9]+$'::pg_catalog.text
      AND NOT (n.nspname OPERATOR(pg_catalog.=) '_avnadmin_monitor'::pg_catalog.name
               AND n.nspowner OPERATOR(pg_catalog.<>) me_role
               AND (r_owner.rolsuper OR r_owner.rolname OPERATOR(pg_catalog.=) '_avnadmin_monitor'::pg_catalog.name)
               AND NOT pg_catalog.pg_has_role(SESSION_USER, n.nspowner, 'MEMBER')
               AND NOT pg_catalog.has_schema_privilege(SESSION_USER, n.oid, 'CREATE'))
  )
  SELECT
      (SELECT pg_catalog.count(*) FROM user_ns n WHERE n.nspname OPERATOR(pg_catalog.<>) 'public'::pg_catalog.name)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.relnamespace
          WHERE x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.pronamespace
          WHERE x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_type x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.typnamespace
          WHERE x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_collation x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.collnamespace
          WHERE x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_operator x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.oprnamespace
          WHERE x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_config x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.cfgnamespace
          WHERE x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_dict x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.dictnamespace
          WHERE x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_conversion x JOIN user_ns n ON n.oid OPERATOR(pg_catalog.=) x.connamespace
          WHERE x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_extension x WHERE x.extname OPERATOR(pg_catalog.<>) 'plpgsql'::pg_catalog.name)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_largeobject_metadata)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_publication)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_subscription x
          WHERE x.subdbid OPERATOR(pg_catalog.=) (SELECT d.oid FROM pg_catalog.pg_database d
                                                WHERE d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()))
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_prepared_xacts x
          WHERE x.database OPERATOR(pg_catalog.=) pg_catalog.current_database())
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_replication_slots x
          WHERE x.database OPERATOR(pg_catalog.=) pg_catalog.current_database())
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_default_acl)
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_db_role_setting s, pg_catalog.unnest(s.setconfig) AS c(cfg)
          WHERE (s.setdatabase OPERATOR(pg_catalog.=) (SELECT d.oid FROM pg_catalog.pg_database d
                                                     WHERE d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database())
                 OR (s.setdatabase OPERATOR(pg_catalog.=) 0::pg_catalog.oid AND s.setrole OPERATOR(pg_catalog.=) me_role))
            AND c.cfg OPERATOR(pg_catalog.!~) '^search_path='::pg_catalog.text)
    INTO foreign_rows;
  IF foreign_rows IS NULL OR foreign_rows OPERATOR(pg_catalog.<>) 0::pg_catalog.int8 THEN
    RAISE EXCEPTION 'FCB_FOREIGN_OBJECTS_AFTER_APPLY';
  END IF;
END
$post$;
