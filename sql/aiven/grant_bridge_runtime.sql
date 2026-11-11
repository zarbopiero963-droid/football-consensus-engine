-- FCB-WP-009 PHASE 1 (workflow mode=apply), after sql/aiven/upsert_bridge_roles.sql: the runtime
-- allowlist for fcb_app on the bridge objects, which are still owned by the administrative
-- identity. Same revokes and grants as the privilege part of finalize_bridge_privileges.sql
-- (tests/test_defaultdb_roles_workflow.py keeps the two identical); fcb_migrator gets nothing
-- here (phase 2 hands it the bridge). Catalog-only DDL: no user code runs.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM fcb_app;
REVOKE ALL ON SCHEMA public FROM fcb_signer;
REVOKE ALL ON SCHEMA public FROM fcb_migrator;
GRANT USAGE ON SCHEMA public TO fcb_app;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, fcb_app, fcb_signer, fcb_migrator;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, fcb_app, fcb_signer, fcb_migrator;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, fcb_app, fcb_signer, fcb_migrator;

GRANT SELECT ON TABLE schema_migrations TO fcb_app;

GRANT SELECT, INSERT, UPDATE ON TABLE
    oauth_authorization_codes,
    oauth_refresh_tokens,
    browser_session_leases,
    api_football_quota_counters
TO fcb_app;
GRANT SELECT, INSERT ON TABLE oauth_access_tokens TO fcb_app;
GRANT SELECT, INSERT, DELETE ON TABLE
    oauth_claude_dcr_clients,
    api_football_quota_leases,
    api_football_quota_attempts,
    sxbet_rate_attempts
TO fcb_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE sxbet_rate_cooldowns TO fcb_app;

-- OAuth persistent throttle (bridge 0009, applied by its own owner-gated workflow): S I U D on
-- oauth_throttle_buckets only, granted only where 0009 is already applied, so on a 0001-0008
-- database this block changes nothing. oauth_throttle_unlock_audit gets no runtime privilege.
--   oauth_throttle_buckets         S I U D  bridge/oauth_persistent_throttle.py acquire (UPSERT) / refund (UPDATE) / GC (DELETE)
DO $throttle$
BEGIN
    IF pg_catalog.to_regclass('public.oauth_throttle_buckets') IS NOT NULL THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.oauth_throttle_buckets TO fcb_app';
    END IF;
END $throttle$;
