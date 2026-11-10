-- FCB-WP-009. Run as the Aiven administrative identity after the three roles have
-- been created through an owner-approved credential workflow. This file never creates
-- credentials and never grants the signer access to the bridge schema.
-- In production it runs only inside the owner-gated roles transaction
-- (.github/workflows/fcb-aiven-defaultdb-roles.yml), after sql/aiven/upsert_bridge_roles.sql
-- gave the administrative identity SET + INHERIT membership in fcb_migrator: a non-superuser
-- (Aiven's avnadmin) may change fcb_migrator's default privileges only with it.
-- Catalog relations and operators are schema-qualified (as the restore files, audit F3).
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name)
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_app'::pg_catalog.name)
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_signer'::pg_catalog.name) THEN
        RAISE EXCEPTION 'FCB_REQUIRED_ROLE_MISSING';
    END IF;
END $roles$;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM fcb_app;
REVOKE ALL ON SCHEMA public FROM fcb_signer;
GRANT USAGE, CREATE ON SCHEMA public TO fcb_migrator;
GRANT USAGE ON SCHEMA public TO fcb_app;

ALTER DEFAULT PRIVILEGES FOR ROLE fcb_migrator IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE fcb_migrator IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE fcb_migrator IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM PUBLIC;
-- WP-009 fix: per-schema default privileges can only ADD to the global ones, so the IN SCHEMA
-- revoke above cannot remove the built-in PUBLIC EXECUTE on new functions (PostgreSQL docs,
-- ALTER DEFAULT PRIVILEGES). Revoke it globally for fcb_migrator: a function a future
-- migration creates is then executable by no one but its owner until explicitly granted.
ALTER DEFAULT PRIVILEGES FOR ROLE fcb_migrator REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
