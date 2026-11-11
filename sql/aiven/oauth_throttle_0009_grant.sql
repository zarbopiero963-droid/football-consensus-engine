-- OAuth persistent throttle: runs in the SAME transaction as aiven_migrations_bridge_0001_0009.sql,
-- as fcb_migrator (the bridge owner after WP-009 phase 2). Grants fcb_app exactly the runtime
-- privileges of the D09-02 matrix on the new table and asserts the end state. Catalog-qualified.
--   oauth_throttle_buckets         S I U D  bridge/oauth_persistent_throttle.py acquire / refund / GC
--   oauth_throttle_unlock_audit    none     (written only by the unlock workflow, as fcb_migrator)
DO $who$
BEGIN
    IF CURRENT_USER OPERATOR(pg_catalog.<>) 'fcb_migrator'::pg_catalog.name
       OR SESSION_USER OPERATOR(pg_catalog.<>) 'fcb_migrator'::pg_catalog.name THEN
        RAISE EXCEPTION 'FCB_THROTTLE_NOT_MIGRATOR';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_app'::pg_catalog.name) THEN
        RAISE EXCEPTION 'FCB_REQUIRED_ROLE_MISSING';
    END IF;
END $who$;

REVOKE ALL ON TABLE public.oauth_throttle_buckets, public.oauth_throttle_unlock_audit FROM PUBLIC;
REVOKE ALL ON SEQUENCE public.oauth_throttle_unlock_audit_audit_id_seq FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.oauth_throttle_buckets TO fcb_app;

DO $assert$
BEGIN
    IF (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid OPERATOR(pg_catalog.=) 'public.oauth_throttle_buckets'::pg_catalog.regclass)
         OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name)
       OR (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid OPERATOR(pg_catalog.=) 'public.oauth_throttle_unlock_audit'::pg_catalog.regclass)
         OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name) THEN
        RAISE EXCEPTION 'FCB_THROTTLE_OWNER_UNEXPECTED';
    END IF;
    IF NOT pg_catalog.has_table_privilege('fcb_app', 'public.oauth_throttle_buckets', 'SELECT,INSERT,UPDATE,DELETE')
       OR pg_catalog.has_table_privilege('fcb_app', 'public.oauth_throttle_buckets', 'TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_table_privilege('fcb_app', 'public.oauth_throttle_unlock_audit', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_sequence_privilege('fcb_app', 'public.oauth_throttle_unlock_audit_audit_id_seq', 'USAGE,SELECT,UPDATE') THEN
        RAISE EXCEPTION 'FCB_THROTTLE_GRANTS_UNEXPECTED';
    END IF;
END $assert$;
