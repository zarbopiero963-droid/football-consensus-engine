-- FCB-WP-005: generated from defaultdb_precheck.sql + defaultdb_precheck_tracked.sql.
-- Runs in the SAME transaction as the restore, after the bridge advisory lock: if the
-- state is not allowed at that instant, the whole transaction aborts before any write.
SELECT pg_advisory_xact_lock(460244120);
DO $fcbguard$
DECLARE s text;
BEGIN
    EXECUTE $fcbq$
WITH expected(kind, relname, def) AS (VALUES
    ('col', 'api_football_quota_attempts', 'api_key_hash text COLLATE pg_catalog.default NOT NULL'),
    ('col', 'api_football_quota_attempts', 'attempt_id text COLLATE pg_catalog.default NOT NULL'),
    ('col', 'api_football_quota_attempts', 'created_at timestamp with time zone NOT NULL DEFAULT clock_timestamp()'),
    ('col', 'api_football_quota_counters', 'api_key_hash text COLLATE pg_catalog.default NOT NULL'),
    ('col', 'api_football_quota_counters', 'bucket bigint NOT NULL'),
    ('col', 'api_football_quota_counters', 'used integer NOT NULL'),
    ('col', 'api_football_quota_counters', 'window_kind text COLLATE pg_catalog.default NOT NULL'),
    ('col', 'api_football_quota_leases', 'api_key_hash text COLLATE pg_catalog.default NOT NULL'),
    ('col', 'api_football_quota_leases', 'expires_at timestamp with time zone NOT NULL'),
    ('col', 'api_football_quota_leases', 'lease_id text COLLATE pg_catalog.default NOT NULL'),
    ('col', 'oauth_claude_dcr_clients', 'client_id_hash text COLLATE pg_catalog.default NOT NULL'),
    ('col', 'oauth_claude_dcr_clients', 'created_at timestamp with time zone NOT NULL DEFAULT now()'),
    ('col', 'oauth_claude_dcr_clients', 'expires_at timestamp with time zone NOT NULL'),
    ('col', 'oauth_claude_dcr_clients', 'redirect_uri text COLLATE pg_catalog.default NOT NULL'),
    ('col', 'sxbet_rate_attempts', 'attempt_id text COLLATE pg_catalog.default NOT NULL'),
    ('col', 'sxbet_rate_attempts', 'bucket_key text COLLATE pg_catalog.default NOT NULL'),
    ('col', 'sxbet_rate_attempts', 'created_at timestamp with time zone NOT NULL DEFAULT clock_timestamp()'),
    ('col', 'sxbet_rate_cooldowns', 'bucket_key text COLLATE pg_catalog.default NOT NULL'),
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
    SELECT c.oid, c.relname, c.relkind, c.relpersistence, c.reloptions, c.relreplident, c.relacl,
           c.relowner, c.relrowsecurity, c.relam
    FROM rel r JOIN runtime_tables t USING (relname) JOIN pg_class c ON c.oid = r.oid
),
got(kind, relname, def) AS (
    SELECT 'col', p.relname, a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
        || CASE WHEN a.attcollation <> 0 THEN ' COLLATE ' || (SELECT cn.nspname || '.' || co.collname FROM pg_collation co JOIN pg_namespace cn ON cn.oid = co.collnamespace WHERE co.oid = a.attcollation) ELSE '' END
        || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
        || COALESCE(' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid), '')
    FROM present p JOIN pg_attribute a ON a.attrelid = p.oid AND a.attnum > 0 AND NOT a.attisdropped
    LEFT JOIN pg_attrdef d ON d.adrelid = p.oid AND d.adnum = a.attnum
    UNION ALL
    SELECT 'con', p.relname, k.conname || ' ' || pg_get_constraintdef(k.oid)
    FROM present p JOIN pg_constraint k ON k.conrelid = p.oid AND k.contype IN ('p', 'u', 'f', 'c', 'x')
    UNION ALL
    SELECT 'idx', p.relname, regexp_replace(pg_get_indexdef(i.indexrelid), ' ON \S+\.', ' ON ')
        || CASE WHEN i.indisvalid AND i.indisready THEN '' ELSE ' INVALID' END
    FROM present p JOIN pg_index i ON i.indrelid = p.oid
),
want AS (SELECT e.* FROM expected e JOIN present p USING (relname)),
facts AS (
    SELECT
        (current_schemas(true))[1] <> 'pg_catalog' AS unsafe_search_path,
        (SELECT count(*) FROM rel) AS relations,
        (SELECT count(*) FROM rel WHERE relname NOT IN (SELECT relname FROM runtime_rel)) AS foreign_relations,
        (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = current_schema()) AS functions,
        (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = current_schema() AND t.typtype IN ('e', 'd', 'r', 'm')) AS user_types,
        (SELECT count(*) FROM (
            (SELECT * FROM got WHERE kind IN ('col', 'con') EXCEPT SELECT * FROM want WHERE kind IN ('col', 'con'))
            UNION ALL
            (SELECT * FROM want WHERE kind IN ('col', 'con') EXCEPT SELECT * FROM got WHERE kind IN ('col', 'con'))
            UNION ALL
            (SELECT * FROM got WHERE kind = 'idx' EXCEPT SELECT * FROM want WHERE kind = 'idx')
        ) diff) AS signature_diffs,
        (SELECT count(*) FROM present WHERE relkind <> 'r' OR relpersistence <> 'p' OR reloptions IS NOT NULL
            OR relreplident <> 'd' OR relacl IS NOT NULL OR relowner <> (SELECT oid FROM pg_roles WHERE rolname = current_user)
            OR relrowsecurity OR relam <> (SELECT oid FROM pg_am WHERE amname = 'heap')) AS table_props,
        (SELECT count(*) FROM present p JOIN pg_attribute a ON a.attrelid = p.oid AND a.attnum > 0
            WHERE a.attacl IS NOT NULL OR a.attoptions IS NOT NULL OR a.attidentity <> '' OR a.attgenerated <> '') AS column_props,
        (SELECT count(*) FROM pg_policy pol JOIN present p ON p.oid = pol.polrelid) AS policies,
        (SELECT count(*) FROM pg_constraint k JOIN present p ON p.oid = k.conrelid
            WHERE NOT k.convalidated) AS unvalidated,
        (SELECT count(*) FROM pg_operator o JOIN pg_namespace n ON n.oid = o.oprnamespace WHERE n.nspname = current_schema())
        + (SELECT count(*) FROM pg_opclass o JOIN pg_namespace n ON n.oid = o.opcnamespace WHERE n.nspname = current_schema())
        + (SELECT count(*) FROM pg_opfamily o JOIN pg_namespace n ON n.oid = o.opfnamespace WHERE n.nspname = current_schema())
        + (SELECT count(*) FROM pg_collation o JOIN pg_namespace n ON n.oid = o.collnamespace WHERE n.nspname = current_schema())
        + (SELECT count(*) FROM pg_conversion o JOIN pg_namespace n ON n.oid = o.connamespace WHERE n.nspname = current_schema())
        + (SELECT count(*) FROM pg_ts_config o JOIN pg_namespace n ON n.oid = o.cfgnamespace WHERE n.nspname = current_schema())
        + (SELECT count(*) FROM pg_ts_dict o JOIN pg_namespace n ON n.oid = o.dictnamespace WHERE n.nspname = current_schema())
        + (SELECT count(*) FROM pg_ts_parser o JOIN pg_namespace n ON n.oid = o.prsnamespace WHERE n.nspname = current_schema())
        + (SELECT count(*) FROM pg_ts_template o JOIN pg_namespace n ON n.oid = o.tmplnamespace WHERE n.nspname = current_schema())
        + (SELECT count(*) FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE n.nspname = current_schema())
          AS catalog_objects,
        (SELECT count(*) FROM pg_default_acl) AS default_acls,
        (SELECT count(*) FROM pg_event_trigger WHERE evtenabled <> 'D') AS event_triggers,
        (SELECT count(*) FROM pg_db_role_setting rs
            WHERE (rs.setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database()) OR rs.setdatabase = 0)
              AND (rs.setrole = 0 OR rs.setrole = (SELECT oid FROM pg_roles WHERE rolname = current_user))
              AND array_to_string(rs.setconfig, ',') LIKE '%search_path%') AS search_path_settings,
        (SELECT count(*) FROM pg_rewrite rw JOIN present p ON p.oid = rw.ev_class) AS rules,
        (SELECT count(*) FROM pg_trigger tg JOIN present p ON p.oid = tg.tgrelid WHERE NOT tg.tgisinternal) AS triggers,
        (SELECT count(*) FROM pg_inherits ih JOIN present p ON p.oid IN (ih.inhrelid, ih.inhparent)) AS inheritance,
        to_regclass(quote_ident(current_schema()) || '.schema_migrations') IS NOT NULL AS tracked,
        to_regclass(quote_ident(current_schema()) || '.signer_schema_migrations') IS NOT NULL AS signer_tracked
)
SELECT CASE
    WHEN unsafe_search_path OR search_path_settings > 0 THEN 'REFUSE_UNSAFE_SEARCH_PATH'
    WHEN event_triggers > 0 THEN 'REFUSE_EVENT_TRIGGERS'
    WHEN default_acls > 0 THEN 'REFUSE_DEFAULT_PRIVILEGES'
    WHEN catalog_objects > 0 THEN 'REFUSE_UNTRACKED_OBJECTS'
    WHEN signer_tracked THEN 'REFUSE_SIGNER_SERIES_PRESENT'
    WHEN signature_diffs > 0 OR table_props > 0 OR column_props > 0 OR policies > 0 OR rules > 0
         OR triggers > 0 OR inheritance > 0 OR unvalidated > 0 THEN 'REFUSE_RUNTIME_TABLE_DRIFT'
    WHEN tracked THEN 'TRACKED'
    WHEN relations = 0 AND functions = 0 AND user_types = 0 THEN 'EMPTY'
    WHEN foreign_relations > 0 OR functions > 0 OR user_types > 0 THEN 'REFUSE_UNTRACKED_OBJECTS'
    ELSE 'RUNTIME_ONLY'
END FROM facts
$fcbq$ INTO s;
    IF s = 'TRACKED' THEN
        EXECUTE $fcbq$
WITH expected(version, name, sha256) AS (VALUES
    (1, '0001_baseline.sql', 'a6cd3bda6f1bfb8f029d4039b2c50d0ed8d444bc12470b28a382faac068f7287'),
    (2, '0002_decision_requests.sql', 'a4a889aaf87ef74a40fb8453b96cbea451b6673ab5dda3eed2e127d906b21012'),
    (3, '0003_browser_session_leases.sql', '4cb5e76853a41fad80ca8f5cb11f5415e3f7dec70a7e351548e00b51bd3e172e'),
    (4, '0004_canonical_entities.sql', '483b2391d9b4af2823f3d74784562777a16e41c4e11c0d9415aea2cafa715a8f'),
    (5, '0005_pilot_server_sealing.sql', 'ee7ad29edc404f38608404216fd55da795eba597fcd6d6eddb7e7f80001fbd56'),
    (6, '0006_evaluation_protocols.sql', '08253596be53ccc1273a7f956793b6c20ae28333c5dc7553c67d8d530acb13fb'),
    (7, '0007_sxbet_rate_limits.sql', '3f3f06d34cf7e83e01a096bd7366eeeecac2edd5d05b1039c491ea64183b74e4'),
    (8, '0008_runtime_tables.sql', '2ee5a7bbe9ce1a8b55474ba73288c1dfda6c847f81b7277bf5956f080dab759d')
),
got AS (SELECT version, name, sha256 FROM schema_migrations),
n AS (SELECT count(*) AS rows, count(DISTINCT version) AS versions FROM got)
SELECT CASE
    WHEN EXISTS (SELECT 1 FROM got WHERE version = 8 AND name = '0008_signer_ledger_anchor.sql')
        THEN 'REFUSE_PRE_SPLIT_SIGNER_0008'
    WHEN (SELECT rows FROM n) = 7 AND (SELECT versions FROM n) = 7 AND NOT EXISTS (
            SELECT version, name, sha256 FROM got
            EXCEPT SELECT version, name, sha256 FROM expected WHERE version <= 7)
        THEN 'BRIDGE_0001_0007'
    WHEN (SELECT rows FROM n) = 8 AND (SELECT versions FROM n) = 8 AND NOT EXISTS (
            SELECT version, name, sha256 FROM got EXCEPT SELECT version, name, sha256 FROM expected)
        THEN 'BRIDGE_0001_0008'
    ELSE 'REFUSE_PARTIAL_OR_DRIFT'
END
$fcbq$ INTO s;
    END IF;
    IF s IS NULL OR s NOT IN ('EMPTY', 'RUNTIME_ONLY', 'BRIDGE_0001_0007', 'BRIDGE_0001_0008') THEN
        RAISE EXCEPTION 'FCB_RESTORE_STATE_REFUSED %', s;
    END IF;
END $fcbguard$;
