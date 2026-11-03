-- FCB-WP-005: read-only restore precheck for the CURRENT schema (public on Aiven).
-- Prints exactly one word: EMPTY or RUNTIME_ONLY may proceed, TRACKED is resolved by
-- defaultdb_precheck_tracked.sql, every REFUSE_* state stops the restore (fail closed).
-- RUNTIME_ONLY = only tables the running code creates itself, each one EXACTLY as the code
-- creates it (columns, types, NOT NULL, defaults, constraints; indexes a subset of the
-- expected ones), plain tables with no rule, trigger, policy, RLS or inheritance. Anything
-- else would be skipped by CREATE TABLE IF NOT EXISTS and silently certified, so it refuses.
-- The signature below was taken from a clean runtime creation; tests keep it in sync.
-- No data row is read.
WITH expected(kind, relname, def) AS (VALUES
    ('col', 'api_football_quota_attempts', 'api_key_hash text NOT NULL'),
    ('col', 'api_football_quota_attempts', 'attempt_id text NOT NULL'),
    ('col', 'api_football_quota_attempts', 'created_at timestamp with time zone NOT NULL DEFAULT clock_timestamp()'),
    ('col', 'api_football_quota_counters', 'api_key_hash text NOT NULL'),
    ('col', 'api_football_quota_counters', 'bucket bigint NOT NULL'),
    ('col', 'api_football_quota_counters', 'used integer NOT NULL'),
    ('col', 'api_football_quota_counters', 'window_kind text NOT NULL'),
    ('col', 'api_football_quota_leases', 'api_key_hash text NOT NULL'),
    ('col', 'api_football_quota_leases', 'expires_at timestamp with time zone NOT NULL'),
    ('col', 'api_football_quota_leases', 'lease_id text NOT NULL'),
    ('col', 'oauth_claude_dcr_clients', 'client_id_hash text NOT NULL'),
    ('col', 'oauth_claude_dcr_clients', 'created_at timestamp with time zone NOT NULL DEFAULT now()'),
    ('col', 'oauth_claude_dcr_clients', 'expires_at timestamp with time zone NOT NULL'),
    ('col', 'oauth_claude_dcr_clients', 'redirect_uri text NOT NULL'),
    ('col', 'sxbet_rate_attempts', 'attempt_id text NOT NULL'),
    ('col', 'sxbet_rate_attempts', 'bucket_key text NOT NULL'),
    ('col', 'sxbet_rate_attempts', 'created_at timestamp with time zone NOT NULL DEFAULT clock_timestamp()'),
    ('col', 'sxbet_rate_cooldowns', 'bucket_key text NOT NULL'),
    ('col', 'sxbet_rate_cooldowns', 'cooldown_until timestamp with time zone NOT NULL'),
    ('col', 'sxbet_rate_cooldowns', 'updated_at timestamp with time zone NOT NULL DEFAULT clock_timestamp()'),
    ('con', 'api_football_quota_attempts', 'api_football_quota_attempts_pkey PRIMARY KEY (attempt_id)'),
    ('con', 'api_football_quota_counters', 'api_football_quota_counters_pkey PRIMARY KEY (api_key_hash, window_kind, bucket)'),
    ('con', 'api_football_quota_leases', 'api_football_quota_leases_pkey PRIMARY KEY (lease_id)'),
    ('con', 'oauth_claude_dcr_clients', 'oauth_claude_dcr_clients_pkey PRIMARY KEY (client_id_hash)'),
    ('con', 'sxbet_rate_attempts', 'sxbet_rate_attempts_bucket_key_check CHECK (((bucket_key ~~ ''sxbet:v1:%''::text) AND (length(bucket_key) <= 256)))'),
    ('con', 'sxbet_rate_attempts', 'sxbet_rate_attempts_pkey PRIMARY KEY (attempt_id)'),
    ('con', 'sxbet_rate_cooldowns', 'sxbet_rate_cooldowns_bucket_key_check CHECK (((bucket_key ~~ ''sxbet:v1:%''::text) AND (length(bucket_key) <= 256)))'),
    ('con', 'sxbet_rate_cooldowns', 'sxbet_rate_cooldowns_pkey PRIMARY KEY (bucket_key)'),
    ('idx', 'api_football_quota_attempts', 'CREATE INDEX idx_api_football_attempt_recent ON api_football_quota_attempts USING btree (api_key_hash, created_at)'),
    ('idx', 'api_football_quota_attempts', 'CREATE UNIQUE INDEX api_football_quota_attempts_pkey ON api_football_quota_attempts USING btree (attempt_id)'),
    ('idx', 'api_football_quota_counters', 'CREATE UNIQUE INDEX api_football_quota_counters_pkey ON api_football_quota_counters USING btree (api_key_hash, window_kind, bucket)'),
    ('idx', 'api_football_quota_leases', 'CREATE INDEX idx_api_football_lease_expires ON api_football_quota_leases USING btree (api_key_hash, expires_at)'),
    ('idx', 'api_football_quota_leases', 'CREATE UNIQUE INDEX api_football_quota_leases_pkey ON api_football_quota_leases USING btree (lease_id)'),
    ('idx', 'oauth_claude_dcr_clients', 'CREATE UNIQUE INDEX oauth_claude_dcr_clients_pkey ON oauth_claude_dcr_clients USING btree (client_id_hash)'),
    ('idx', 'sxbet_rate_attempts', 'CREATE INDEX idx_sxbet_rate_attempts_bucket ON sxbet_rate_attempts USING btree (bucket_key, created_at)'),
    ('idx', 'sxbet_rate_attempts', 'CREATE INDEX idx_sxbet_rate_attempts_created ON sxbet_rate_attempts USING btree (created_at)'),
    ('idx', 'sxbet_rate_attempts', 'CREATE UNIQUE INDEX sxbet_rate_attempts_pkey ON sxbet_rate_attempts USING btree (attempt_id)'),
    ('idx', 'sxbet_rate_cooldowns', 'CREATE UNIQUE INDEX sxbet_rate_cooldowns_pkey ON sxbet_rate_cooldowns USING btree (bucket_key)')
),
runtime_tables(relname) AS (SELECT DISTINCT relname FROM expected),
runtime_rel(relname) AS (
    SELECT relname FROM runtime_tables
    UNION SELECT substring(def FROM '^CREATE (?:UNIQUE )?INDEX (\S+) ') FROM expected WHERE kind = 'idx'
),
rel AS (
    SELECT c.oid, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f', 'i', 'I', 'c')
),
present AS (
    SELECT r.oid, r.relname, r.relkind FROM rel r JOIN runtime_tables t USING (relname)
),
got(kind, relname, def) AS (
    SELECT 'col', p.relname, a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
        || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
        || COALESCE(' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid), '')
    FROM present p JOIN pg_attribute a ON a.attrelid = p.oid AND a.attnum > 0 AND NOT a.attisdropped
    LEFT JOIN pg_attrdef d ON d.adrelid = p.oid AND d.adnum = a.attnum
    UNION ALL
    SELECT 'con', p.relname, k.conname || ' ' || pg_get_constraintdef(k.oid)
    FROM present p JOIN pg_constraint k ON k.conrelid = p.oid AND k.contype IN ('p', 'u', 'f', 'c', 'x')
    UNION ALL
    SELECT 'idx', p.relname, regexp_replace(pg_get_indexdef(i.indexrelid), ' ON \S+\.', ' ON ')
    FROM present p JOIN pg_index i ON i.indrelid = p.oid
),
want AS (SELECT e.* FROM expected e JOIN present p USING (relname)),
facts AS (
    SELECT
        (SELECT count(*) FROM rel) AS relations,
        (SELECT count(*) FROM rel WHERE relname NOT IN (SELECT relname FROM runtime_rel)) AS foreign_relations,
        (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = current_schema()) AS functions,
        (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = current_schema() AND t.typtype IN ('e', 'd', 'r', 'm')) AS user_types,
        -- columns and constraints must match exactly; indexes may only be a subset
        (SELECT count(*) FROM (
            (SELECT * FROM got WHERE kind IN ('col', 'con') EXCEPT SELECT * FROM want WHERE kind IN ('col', 'con'))
            UNION ALL
            (SELECT * FROM want WHERE kind IN ('col', 'con') EXCEPT SELECT * FROM got WHERE kind IN ('col', 'con'))
            UNION ALL
            (SELECT * FROM got WHERE kind = 'idx' EXCEPT SELECT * FROM want WHERE kind = 'idx')
        ) diff) AS signature_diffs,
        (SELECT count(*) FROM present WHERE relkind <> 'r') AS not_plain,
        (SELECT count(*) FROM present p JOIN pg_class c ON c.oid = p.oid WHERE c.relrowsecurity) AS rls,
        (SELECT count(*) FROM pg_policy pol JOIN present p ON p.oid = pol.polrelid) AS policies,
        (SELECT count(*) FROM pg_rewrite rw JOIN present p ON p.oid = rw.ev_class) AS rules,
        (SELECT count(*) FROM pg_trigger tg JOIN present p ON p.oid = tg.tgrelid WHERE NOT tg.tgisinternal) AS triggers,
        (SELECT count(*) FROM pg_inherits ih JOIN present p ON p.oid IN (ih.inhrelid, ih.inhparent)) AS inheritance,
        to_regclass(quote_ident(current_schema()) || '.schema_migrations') IS NOT NULL AS tracked,
        to_regclass(quote_ident(current_schema()) || '.signer_schema_migrations') IS NOT NULL AS signer_tracked
)
SELECT CASE
    WHEN signer_tracked THEN 'REFUSE_SIGNER_SERIES_PRESENT'
    WHEN tracked THEN 'TRACKED'
    WHEN relations = 0 AND functions = 0 AND user_types = 0 THEN 'EMPTY'
    WHEN foreign_relations > 0 OR functions > 0 OR user_types > 0 THEN 'REFUSE_UNTRACKED_OBJECTS'
    WHEN signature_diffs > 0 OR not_plain > 0 OR rls > 0 OR policies > 0 OR rules > 0
         OR triggers > 0 OR inheritance > 0 THEN 'REFUSE_RUNTIME_TABLE_DRIFT'
    ELSE 'RUNTIME_ONLY'
END FROM facts;
