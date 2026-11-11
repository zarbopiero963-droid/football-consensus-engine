-- OAuth persistent throttle: emergency unlock (owner decision D05), run by the protected workflow
-- .github/workflows/fcb-aiven-defaultdb-oauth-throttle-unlock.yml as fcb_migrator in ONE
-- transaction (psql -1). Limited scope: one scope, and either its global bucket (target 'global')
-- or ONE exact key hash (target 'key'). Every removed bucket is first copied to
-- oauth_throttle_unlock_audit with the GitHub run id and actor (D11: evidence is kept, never
-- deleted); one summary row is written even when nothing matched. Variables come from psql -v
-- and are only used as quoted literals (:'name'). Catalog-qualified.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SET LOCAL search_path = public;

DO $who$
BEGIN
    IF CURRENT_USER OPERATOR(pg_catalog.<>) 'fcb_migrator'::pg_catalog.name
       OR SESSION_USER OPERATOR(pg_catalog.<>) 'fcb_migrator'::pg_catalog.name THEN
        RAISE EXCEPTION 'FCB_THROTTLE_NOT_MIGRATOR';
    END IF;
    IF pg_catalog.to_regclass('public.oauth_throttle_buckets') IS NULL
       OR pg_catalog.to_regclass('public.oauth_throttle_unlock_audit') IS NULL THEN
        RAISE EXCEPTION 'FCB_THROTTLE_TABLES_MISSING';
    END IF;
END $who$;

INSERT INTO public.oauth_throttle_unlock_audit (scope, target, github_run_id, github_actor)
VALUES (:'unlock_scope', :'unlock_target', :'github_run_id', :'github_actor');

WITH victims AS (
    SELECT b.scope, b.key_hash, b.tat, b.updated_at
    FROM public.oauth_throttle_buckets b
    WHERE b.scope OPERATOR(pg_catalog.=) :'unlock_scope'
      AND b.key_hash OPERATOR(pg_catalog.=) :'unlock_key_hash'
    FOR UPDATE
), audited AS (
    INSERT INTO public.oauth_throttle_unlock_audit
        (scope, target, github_run_id, github_actor, removed_key_hash, removed_tat, removed_updated_at)
    SELECT v.scope, :'unlock_target', :'github_run_id', :'github_actor', v.key_hash, v.tat, v.updated_at
    FROM victims v
    RETURNING removed_key_hash
)
DELETE FROM public.oauth_throttle_buckets b
USING audited a
WHERE b.scope OPERATOR(pg_catalog.=) :'unlock_scope'
  AND b.key_hash OPERATOR(pg_catalog.=) a.removed_key_hash;
