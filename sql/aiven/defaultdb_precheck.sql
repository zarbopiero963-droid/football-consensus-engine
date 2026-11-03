-- FCB-WP-005: read-only restore precheck for the CURRENT schema (public on Aiven).
-- Prints exactly one word: EMPTY or RUNTIME_ONLY may proceed, TRACKED is resolved by
-- defaultdb_precheck_tracked.sql, every REFUSE_* state stops the restore (fail closed).
-- RUNTIME_ONLY = only the tables the running code creates itself (identical DDL, IF NOT EXISTS
-- in 0007/0008). No data row is read.
WITH runtime_rel(relname) AS (VALUES
    ('sxbet_rate_attempts'), ('sxbet_rate_attempts_pkey'), ('idx_sxbet_rate_attempts_bucket'),
    ('idx_sxbet_rate_attempts_created'), ('sxbet_rate_cooldowns'), ('sxbet_rate_cooldowns_pkey'),
    ('api_football_quota_counters'), ('api_football_quota_counters_pkey'),
    ('api_football_quota_leases'), ('api_football_quota_leases_pkey'), ('idx_api_football_lease_expires'),
    ('api_football_quota_attempts'), ('api_football_quota_attempts_pkey'), ('idx_api_football_attempt_recent'),
    ('oauth_claude_dcr_clients'), ('oauth_claude_dcr_clients_pkey')
),
rel AS (
    SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f', 'i', 'I', 'c')
),
facts AS (
    SELECT
        (SELECT count(*) FROM rel) AS relations,
        (SELECT count(*) FROM rel WHERE relname NOT IN (SELECT relname FROM runtime_rel)) AS foreign_relations,
        (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = current_schema()) AS functions,
        (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = current_schema() AND t.typtype IN ('e', 'd', 'r', 'm')) AS user_types,
        to_regclass(quote_ident(current_schema()) || '.schema_migrations') IS NOT NULL AS tracked,
        to_regclass(quote_ident(current_schema()) || '.signer_schema_migrations') IS NOT NULL AS signer_tracked
)
SELECT CASE
    WHEN signer_tracked THEN 'REFUSE_SIGNER_SERIES_PRESENT'
    WHEN NOT tracked AND relations = 0 AND functions = 0 AND user_types = 0 THEN 'EMPTY'
    WHEN NOT tracked AND foreign_relations = 0 AND functions = 0 AND user_types = 0 THEN 'RUNTIME_ONLY'
    WHEN NOT tracked THEN 'REFUSE_UNTRACKED_OBJECTS'
    ELSE 'TRACKED'
END FROM facts;
