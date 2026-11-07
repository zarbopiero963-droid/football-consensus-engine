-- FCB-WP-009. Run as the Aiven administrative identity after the three roles have
-- been created through an owner-approved credential workflow. This file never creates
-- credentials and never grants the signer access to the bridge schema.
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fcb_migrator')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fcb_app')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fcb_signer') THEN
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
