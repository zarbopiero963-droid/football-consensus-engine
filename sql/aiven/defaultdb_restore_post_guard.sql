-- FCB-WP-005: generated. Runs in the SAME transaction, AFTER the bridge script: the session
-- conditions must be unchanged (audit F3), the end state must be BRIDGE_0001_0008 with every
-- check of the precheck passing, the catalog fingerprint must equal :'expected_fingerprint'
-- (pinned in the workflow, PostgreSQL 18), and after an EMPTY prior state every object must
-- carry this transaction's xmin (audit F7). Otherwise the transaction aborts and nothing is
-- committed. Residual: an object a concurrent session commits after this statement.
DO $fcbsession$
BEGIN
    IF pg_catalog.current_schema() IS NULL OR pg_catalog.current_schema() OPERATOR(pg_catalog.<>) 'public'::pg_catalog.name OR (pg_catalog.current_schemas(true))[1] OPERATOR(pg_catalog.<>) 'pg_catalog'::pg_catalog.name OR pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) 'public'::pg_catalog.text OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER OR EXISTS (SELECT 1 FROM pg_catalog.pg_settings g WHERE g.source OPERATOR(pg_catalog.=) ANY (ARRAY['database', 'user', 'database user']::pg_catalog.text[]) AND g.name OPERATOR(pg_catalog.<>) 'search_path'::pg_catalog.text) OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_roles r_owner ON r_owner.oid OPERATOR(pg_catalog.=) n.nspowner WHERE n.nspname OPERATOR(pg_catalog.<>) ALL (ARRAY['public', 'pg_catalog', 'information_schema', 'pg_toast']::pg_catalog.name[]) AND n.nspname OPERATOR(pg_catalog.!~) '^pg_(toast_)?temp_[0-9]+$'::pg_catalog.text AND NOT (n.nspname OPERATOR(pg_catalog.=) '_avnadmin_monitor'::pg_catalog.name AND n.nspowner OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER) AND (r_owner.rolsuper OR r_owner.rolname OPERATOR(pg_catalog.=) '_avnadmin_monitor'::pg_catalog.name) AND NOT pg_catalog.pg_has_role(SESSION_USER, n.nspowner, 'MEMBER') AND NOT pg_catalog.has_schema_privilege(SESSION_USER, n.oid, 'CREATE'))) THEN
        RAISE EXCEPTION 'FCB_RESTORE_SESSION_GUARD_FAILED';
    END IF;
END $fcbsession$;
SET LOCAL search_path = public;
SELECT pg_catalog.set_config('fcb.expected_fingerprint', :'expected_fingerprint', true) IS NOT NULL AS expected_set;
DO $fcbpost$
DECLARE
    s pg_catalog.text; f pg_catalog.text; foreign_rows pg_catalog.int8;
    prior pg_catalog.text := pg_catalog.current_setting('fcb.prior_state', true);
    me_xid pg_catalog.text := pg_catalog.pg_current_xact_id()::pg_catalog.xid::pg_catalog.text;
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
bridge_functions(sig) AS (VALUES
    ('canonical_alias_revision_guard||trigger|plpgsql|false|-|70c78d2f97f2966f7611a87dfb58f946'),
    ('canonical_reject_mutation||trigger|plpgsql|false|-|1ed4bc2ff6613135008b8adc6211fd3f'),
    ('seal_evaluation_protocol||trigger|plpgsql|false|-|7377c626fd29b2fd4ef38c13d35bb323'),
    ('seal_pilot_row||trigger|plpgsql|false|-|fe277f0013ff9ac7ff637742361dbca4')
),
bridge_triggers(sig) AS (VALUES
    ('canonical_alias_versions|canonical_alias_version_insert|canonical_alias_revision_guard|7'),
    ('canonical_alias_versions|canonical_aliases_immutable|canonical_reject_mutation|58'),
    ('canonical_entities|canonical_entities_immutable|canonical_reject_mutation|58'),
    ('evaluation_protocols|evaluation_protocols_no_truncate|seal_evaluation_protocol|34'),
    ('evaluation_protocols|evaluation_protocols_seal|seal_evaluation_protocol|31'),
    ('source_pilot_predictions|source_pilot_predictions_no_truncate|seal_pilot_row|34'),
    ('source_pilot_predictions|source_pilot_predictions_seal|seal_pilot_row|31'),
    ('source_pilot_runs|source_pilot_runs_no_truncate|seal_pilot_row|34'),
    ('source_pilot_runs|source_pilot_runs_seal|seal_pilot_row|31')
),
ns AS (SELECT n.oid FROM pg_catalog.pg_namespace n WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name),
me AS (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) CURRENT_USER),
sess AS (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER),
db AS (SELECT d.oid FROM pg_catalog.pg_database d WHERE d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()),
runtime_tables(relname) AS (SELECT DISTINCT e.relname FROM expected e),
runtime_rel(relname) AS (
    SELECT t.relname FROM runtime_tables t
    UNION SELECT pg_catalog.substring(e.def, '^CREATE (?:UNIQUE )?INDEX (\S+) '::pg_catalog.text) FROM expected e
        WHERE e.kind OPERATOR(pg_catalog.=) 'idx'::pg_catalog.text
),
allrel AS (
    SELECT c.oid, c.relname::pg_catalog.text AS relname, c.relkind, c.reltype, c.relrowsecurity, c.relforcerowsecurity
    FROM pg_catalog.pg_class c JOIN ns ON ns.oid OPERATOR(pg_catalog.=) c.relnamespace
),
rel AS (
    SELECT a.oid, a.relname, a.relkind FROM allrel a
    WHERE a.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'v', 'm', 'S', 'f', 'i', 'I', 'c']::pg_catalog."char"[])
),
present AS (
    SELECT c.oid, r.relname, c.relkind, c.relpersistence, c.reloptions, c.relreplident, c.relacl,
           c.relowner, c.relrowsecurity, c.relam
    FROM rel r JOIN runtime_tables t ON t.relname OPERATOR(pg_catalog.=) r.relname
    JOIN pg_catalog.pg_class c ON c.oid OPERATOR(pg_catalog.=) r.oid
),
got(kind, relname, def) AS (
    SELECT 'col'::pg_catalog.text, p.relname, a.attname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) pg_catalog.format_type(a.atttypid, a.atttypmod)
        OPERATOR(pg_catalog.||) CASE WHEN a.attcollation OPERATOR(pg_catalog.<>) 0::pg_catalog.oid THEN ' COLLATE '::pg_catalog.text
            OPERATOR(pg_catalog.||) (SELECT cn.nspname::pg_catalog.text OPERATOR(pg_catalog.||) '.'::pg_catalog.text
                                     OPERATOR(pg_catalog.||) co.collname::pg_catalog.text
                                     FROM pg_catalog.pg_collation co JOIN pg_catalog.pg_namespace cn ON cn.oid OPERATOR(pg_catalog.=) co.collnamespace
                                     WHERE co.oid OPERATOR(pg_catalog.=) a.attcollation) ELSE ''::pg_catalog.text END
        OPERATOR(pg_catalog.||) CASE WHEN a.attnotnull THEN ' NOT NULL'::pg_catalog.text ELSE ''::pg_catalog.text END
        OPERATOR(pg_catalog.||) COALESCE(' DEFAULT '::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.pg_get_expr(d.adbin, d.adrelid), ''::pg_catalog.text)
    FROM present p JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) p.oid
        AND a.attnum OPERATOR(pg_catalog.>) 0::pg_catalog.int2 AND NOT a.attisdropped
    LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid OPERATOR(pg_catalog.=) p.oid AND d.adnum OPERATOR(pg_catalog.=) a.attnum
    UNION ALL
    SELECT 'con'::pg_catalog.text, p.relname, k.conname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) pg_catalog.pg_get_constraintdef(k.oid)
    FROM present p JOIN pg_catalog.pg_constraint k ON k.conrelid OPERATOR(pg_catalog.=) p.oid
        AND k.contype OPERATOR(pg_catalog.=) ANY (ARRAY['p', 'u', 'f', 'c', 'x']::pg_catalog."char"[])
    UNION ALL
    SELECT 'idx'::pg_catalog.text, p.relname,
        pg_catalog.regexp_replace(pg_catalog.pg_get_indexdef(i.indexrelid), ' ON \S+\.'::pg_catalog.text, ' ON '::pg_catalog.text)
        OPERATOR(pg_catalog.||) CASE WHEN i.indisvalid AND i.indisready THEN ''::pg_catalog.text ELSE ' INVALID'::pg_catalog.text END
    FROM present p JOIN pg_catalog.pg_index i ON i.indrelid OPERATOR(pg_catalog.=) p.oid
),
want AS (SELECT e.* FROM expected e WHERE EXISTS (SELECT 1 FROM present p WHERE p.relname OPERATOR(pg_catalog.=) e.relname)),
user_ns AS (
    SELECT n.oid, n.nspname FROM pg_catalog.pg_namespace n
    JOIN pg_catalog.pg_roles r_owner ON r_owner.oid OPERATOR(pg_catalog.=) n.nspowner
    WHERE n.nspname OPERATOR(pg_catalog.<>) ALL (ARRAY['pg_catalog', 'information_schema', 'pg_toast']::pg_catalog.name[])
      AND n.nspname OPERATOR(pg_catalog.!~) '^pg_(toast_)?temp_[0-9]+$'::pg_catalog.text
      AND NOT (n.nspname OPERATOR(pg_catalog.=) '_avnadmin_monitor'::pg_catalog.name
               AND n.nspowner OPERATOR(pg_catalog.<>) (SELECT sess.oid FROM sess)
               AND (r_owner.rolsuper OR r_owner.rolname OPERATOR(pg_catalog.=) '_avnadmin_monitor'::pg_catalog.name)
               AND NOT pg_catalog.pg_has_role(SESSION_USER, n.nspowner, 'MEMBER')
               AND NOT pg_catalog.has_schema_privilege(SESSION_USER, n.oid, 'CREATE'))
),
facts AS (
    SELECT
        (pg_catalog.current_schemas(true))[1] OPERATOR(pg_catalog.<>) 'pg_catalog'::pg_catalog.name
            OR pg_catalog.current_schema() IS NULL
            OR pg_catalog.current_schema() OPERATOR(pg_catalog.<>) 'public'::pg_catalog.name
            OR NOT EXISTS (SELECT 1 FROM ns) AS unsafe_search_path,
        (SELECT pg_catalog.count(*) FROM user_ns u WHERE u.nspname OPERATOR(pg_catalog.<>) 'public'::pg_catalog.name) AS unexpected_schemas,
        (SELECT pg_catalog.count(*) FROM rel) AS relations,
        (SELECT pg_catalog.count(*) FROM rel r WHERE NOT EXISTS (
            SELECT 1 FROM runtime_rel x WHERE x.relname OPERATOR(pg_catalog.=) r.relname)) AS foreign_relations,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace) AS functions,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_type t JOIN ns ON ns.oid OPERATOR(pg_catalog.=) t.typnamespace) AS types,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace
            JOIN pg_catalog.pg_language l ON l.oid OPERATOR(pg_catalog.=) p.prolang
            WHERE p.prokind OPERATOR(pg_catalog.<>) 'f'::pg_catalog."char"
               OR NOT EXISTS (SELECT 1 FROM bridge_functions b WHERE b.sig OPERATOR(pg_catalog.=) (
                    p.proname::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) pg_catalog.pg_get_function_identity_arguments(p.oid) OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) pg_catalog.format_type(p.prorettype, NULL::pg_catalog.int4) OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) l.lanname::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) p.prosecdef::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) COALESCE(p.proconfig::pg_catalog.text, '-'::pg_catalog.text) OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) pg_catalog.md5(p.prosrc)))) AS foreign_functions,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_type t JOIN ns ON ns.oid OPERATOR(pg_catalog.=) t.typnamespace
            WHERE NOT EXISTS (SELECT 1 FROM allrel c WHERE c.reltype OPERATOR(pg_catalog.<>) 0::pg_catalog.oid
                AND (c.reltype OPERATOR(pg_catalog.=) t.oid
                     OR EXISTS (SELECT 1 FROM pg_catalog.pg_type e WHERE e.oid OPERATOR(pg_catalog.=) c.reltype
                                AND e.typarray OPERATOR(pg_catalog.=) t.oid)))) AS foreign_types,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger tg JOIN allrel c ON c.oid OPERATOR(pg_catalog.=) tg.tgrelid
            JOIN pg_catalog.pg_proc f ON f.oid OPERATOR(pg_catalog.=) tg.tgfoid
            WHERE NOT tg.tgisinternal AND (
                f.pronamespace OPERATOR(pg_catalog.<>) (SELECT ns.oid FROM ns)
                OR tg.tgenabled OPERATOR(pg_catalog.<>) 'O'::pg_catalog."char"
                OR tg.tgnargs OPERATOR(pg_catalog.<>) 0::pg_catalog.int2
                OR tg.tgattr::pg_catalog.text OPERATOR(pg_catalog.<>) ''::pg_catalog.text
                OR tg.tgqual IS NOT NULL
                OR tg.tgconstraint OPERATOR(pg_catalog.<>) 0::pg_catalog.oid
                OR tg.tgoldtable IS NOT NULL OR tg.tgnewtable IS NOT NULL
                OR NOT EXISTS (SELECT 1 FROM bridge_triggers b WHERE b.sig OPERATOR(pg_catalog.=) (
                    c.relname OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) tg.tgname::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) f.proname::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) tg.tgtype::pg_catalog.text)))) AS foreign_triggers,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_rewrite rw JOIN allrel c ON c.oid OPERATOR(pg_catalog.=) rw.ev_class)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_policy pol JOIN allrel c ON c.oid OPERATOR(pg_catalog.=) pol.polrelid)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM allrel c WHERE c.relrowsecurity OR c.relforcerowsecurity)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_inherits ih
            WHERE EXISTS (SELECT 1 FROM allrel c WHERE c.oid OPERATOR(pg_catalog.=) ih.inhrelid OR c.oid OPERATOR(pg_catalog.=) ih.inhparent))
          AS relation_hooks,
        (SELECT pg_catalog.count(*) FROM (
            (SELECT * FROM got WHERE got.kind OPERATOR(pg_catalog.=) ANY (ARRAY['col', 'con']::pg_catalog.text[])
             EXCEPT SELECT * FROM want WHERE want.kind OPERATOR(pg_catalog.=) ANY (ARRAY['col', 'con']::pg_catalog.text[]))
            UNION ALL
            (SELECT * FROM want WHERE want.kind OPERATOR(pg_catalog.=) ANY (ARRAY['col', 'con']::pg_catalog.text[])
             EXCEPT SELECT * FROM got WHERE got.kind OPERATOR(pg_catalog.=) ANY (ARRAY['col', 'con']::pg_catalog.text[]))
            UNION ALL
            (SELECT * FROM got WHERE got.kind OPERATOR(pg_catalog.=) 'idx'::pg_catalog.text
             EXCEPT SELECT * FROM want WHERE want.kind OPERATOR(pg_catalog.=) 'idx'::pg_catalog.text)
        ) diff) AS signature_diffs,
        (SELECT pg_catalog.count(*) FROM present p WHERE p.relkind OPERATOR(pg_catalog.<>) 'r'::pg_catalog."char"
            OR p.relpersistence OPERATOR(pg_catalog.<>) 'p'::pg_catalog."char" OR p.reloptions IS NOT NULL
            OR p.relreplident OPERATOR(pg_catalog.<>) 'd'::pg_catalog."char" OR NOT (p.relacl IS NULL OR (p.relname::pg_catalog.text OPERATOR(pg_catalog.=) ANY (ARRAY['api_football_quota_counters', 'api_football_quota_leases', 'api_football_quota_attempts']::pg_catalog.text[]) AND (SELECT pg_catalog.string_agg(z.e, ','::pg_catalog.text ORDER BY z.e) FROM (SELECT x.grantor::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.grantee::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e FROM pg_catalog.aclexplode(p.relacl) x) z) OPERATOR(pg_catalog.=) (SELECT pg_catalog.string_agg(z.e, ','::pg_catalog.text ORDER BY z.e) FROM (SELECT x.grantor::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.grantee::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e FROM pg_catalog.aclexplode(pg_catalog.acldefault('r'::pg_catalog."char", p.relowner)) x UNION ALL SELECT p.relowner::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) w.oid::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) pv.pv OPERATOR(pg_catalog.||) '/false'::pg_catalog.text FROM (SELECT r.oid AS oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb AND NOT r.rolbypassrls AND NOT r.rolreplication AND NOT r.rolinherit AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member OPERATOR(pg_catalog.=) r.oid)) w, pg_catalog.unnest(CASE WHEN p.relname::pg_catalog.text OPERATOR(pg_catalog.=) 'api_football_quota_counters'::pg_catalog.text THEN ARRAY['INSERT', 'SELECT']::pg_catalog.text[] ELSE ARRAY['DELETE', 'INSERT', 'SELECT']::pg_catalog.text[] END) pv) z)))
            OR p.relowner OPERATOR(pg_catalog.<>) (SELECT me.oid FROM me)
            OR p.relrowsecurity
            OR p.relam OPERATOR(pg_catalog.<>) (SELECT am.oid FROM pg_catalog.pg_am am WHERE am.amname OPERATOR(pg_catalog.=) 'heap'::pg_catalog.name)) AS table_props,
        (SELECT pg_catalog.count(*) FROM present p JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) p.oid
            AND a.attnum OPERATOR(pg_catalog.>) 0::pg_catalog.int2
            WHERE NOT (a.attacl IS NULL OR (p.relname::pg_catalog.text OPERATOR(pg_catalog.=) 'api_football_quota_counters'::pg_catalog.text AND a.attname OPERATOR(pg_catalog.=) 'used'::pg_catalog.name AND (SELECT pg_catalog.string_agg(z.e, ','::pg_catalog.text ORDER BY z.e) FROM (SELECT x.grantor::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.grantee::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e FROM pg_catalog.aclexplode(a.attacl) x) z) OPERATOR(pg_catalog.=) (SELECT (SELECT o.relowner FROM pg_catalog.pg_class o WHERE o.oid OPERATOR(pg_catalog.=) a.attrelid)::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) w.oid::pg_catalog.text OPERATOR(pg_catalog.||) '/UPDATE/false'::pg_catalog.text FROM (SELECT r.oid AS oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb AND NOT r.rolbypassrls AND NOT r.rolreplication AND NOT r.rolinherit AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member OPERATOR(pg_catalog.=) r.oid)) w))) OR a.attoptions IS NOT NULL
               OR a.attidentity OPERATOR(pg_catalog.<>) ''::pg_catalog."char"
               OR a.attgenerated OPERATOR(pg_catalog.<>) ''::pg_catalog."char") AS column_props,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_policy pol JOIN present p ON p.oid OPERATOR(pg_catalog.=) pol.polrelid) AS policies,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_constraint k JOIN present p ON p.oid OPERATOR(pg_catalog.=) k.conrelid
            WHERE NOT k.convalidated) AS unvalidated,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_operator o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.oprnamespace)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_opclass o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.opcnamespace)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_opfamily o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.opfnamespace)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_collation o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.collnamespace)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_conversion o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.connamespace)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_config o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.cfgnamespace)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_dict o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.dictnamespace)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_parser o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.prsnamespace)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_template o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.tmplnamespace)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_extension e JOIN ns ON ns.oid OPERATOR(pg_catalog.=) e.extnamespace)
          AS catalog_objects,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace
            WHERE p.prokind OPERATOR(pg_catalog.<>) 'f'::pg_catalog."char") AS non_function_routines,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_publication) AS publications,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_default_acl) AS default_acls,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_event_trigger et
            WHERE et.evtenabled OPERATOR(pg_catalog.<>) 'D'::pg_catalog."char") AS event_triggers,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_db_role_setting rs
            WHERE (rs.setdatabase OPERATOR(pg_catalog.=) (SELECT db.oid FROM db) OR rs.setdatabase OPERATOR(pg_catalog.=) 0::pg_catalog.oid)
              AND (rs.setrole OPERATOR(pg_catalog.=) 0::pg_catalog.oid OR rs.setrole OPERATOR(pg_catalog.=) (SELECT me.oid FROM me))
              AND pg_catalog.array_to_string(rs.setconfig, ','::pg_catalog.text) OPERATOR(pg_catalog.~~) '%search_path%'::pg_catalog.text) AS search_path_settings,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_db_role_setting rs, pg_catalog.unnest(rs.setconfig) AS cfg(c)
            WHERE (rs.setdatabase OPERATOR(pg_catalog.=) (SELECT db.oid FROM db)
                   OR (rs.setdatabase OPERATOR(pg_catalog.=) 0::pg_catalog.oid
                       AND (rs.setrole OPERATOR(pg_catalog.=) 0::pg_catalog.oid OR rs.setrole OPERATOR(pg_catalog.=) (SELECT me.oid FROM me)
                            OR rs.setrole OPERATOR(pg_catalog.=) (SELECT sess.oid FROM sess))))
              AND cfg.c OPERATOR(pg_catalog.!~) '^search_path='::pg_catalog.text) AS other_settings,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_extension x WHERE x.extname OPERATOR(pg_catalog.<>) 'plpgsql'::pg_catalog.name)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_largeobject_metadata)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_subscription x
            WHERE x.subdbid OPERATOR(pg_catalog.=) (SELECT db.oid FROM db))
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_prepared_xacts x
            WHERE x.database OPERATOR(pg_catalog.=) pg_catalog.current_database())
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_replication_slots x
            WHERE x.database OPERATOR(pg_catalog.=) pg_catalog.current_database()) AS database_occupancy,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_rewrite rw JOIN present p ON p.oid OPERATOR(pg_catalog.=) rw.ev_class) AS rules,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger tg JOIN present p ON p.oid OPERATOR(pg_catalog.=) tg.tgrelid
            WHERE NOT tg.tgisinternal) AS triggers,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_inherits ih JOIN present p
            ON p.oid OPERATOR(pg_catalog.=) ih.inhrelid OR p.oid OPERATOR(pg_catalog.=) ih.inhparent) AS inheritance,
        EXISTS (SELECT 1 FROM allrel c WHERE c.relname OPERATOR(pg_catalog.=) 'schema_migrations'::pg_catalog.text) AS tracked,
        EXISTS (SELECT 1 FROM allrel c WHERE c.relname OPERATOR(pg_catalog.=) 'signer_schema_migrations'::pg_catalog.text) AS signer_tracked
)
SELECT CASE
    WHEN unsafe_search_path OR search_path_settings OPERATOR(pg_catalog.>) 0::pg_catalog.int8 THEN 'REFUSE_UNSAFE_SEARCH_PATH'
    WHEN other_settings OPERATOR(pg_catalog.>) 0::pg_catalog.int8 THEN 'REFUSE_UNSAFE_SETTINGS'
    WHEN unexpected_schemas OPERATOR(pg_catalog.>) 0::pg_catalog.int8 THEN 'REFUSE_UNEXPECTED_SCHEMAS'
    WHEN event_triggers OPERATOR(pg_catalog.>) 0::pg_catalog.int8 THEN 'REFUSE_EVENT_TRIGGERS'
    WHEN default_acls OPERATOR(pg_catalog.>) 0::pg_catalog.int8 THEN 'REFUSE_DEFAULT_PRIVILEGES'
    WHEN catalog_objects OPERATOR(pg_catalog.>) 0::pg_catalog.int8 OR non_function_routines OPERATOR(pg_catalog.>) 0::pg_catalog.int8
         OR publications OPERATOR(pg_catalog.>) 0::pg_catalog.int8 THEN 'REFUSE_UNTRACKED_OBJECTS'
    WHEN signer_tracked THEN 'REFUSE_SIGNER_SERIES_PRESENT'
    WHEN signature_diffs OPERATOR(pg_catalog.>) 0::pg_catalog.int8 OR table_props OPERATOR(pg_catalog.>) 0::pg_catalog.int8
         OR column_props OPERATOR(pg_catalog.>) 0::pg_catalog.int8 OR policies OPERATOR(pg_catalog.>) 0::pg_catalog.int8
         OR rules OPERATOR(pg_catalog.>) 0::pg_catalog.int8 OR triggers OPERATOR(pg_catalog.>) 0::pg_catalog.int8
         OR inheritance OPERATOR(pg_catalog.>) 0::pg_catalog.int8 OR unvalidated OPERATOR(pg_catalog.>) 0::pg_catalog.int8
         THEN 'REFUSE_RUNTIME_TABLE_DRIFT'
    WHEN foreign_functions OPERATOR(pg_catalog.>) 0::pg_catalog.int8 OR foreign_types OPERATOR(pg_catalog.>) 0::pg_catalog.int8
         OR foreign_triggers OPERATOR(pg_catalog.>) 0::pg_catalog.int8 OR relation_hooks OPERATOR(pg_catalog.>) 0::pg_catalog.int8
         THEN 'REFUSE_UNTRACKED_OBJECTS'
    WHEN tracked THEN 'TRACKED'
    WHEN relations OPERATOR(pg_catalog.=) 0::pg_catalog.int8 AND functions OPERATOR(pg_catalog.=) 0::pg_catalog.int8
         AND types OPERATOR(pg_catalog.=) 0::pg_catalog.int8 AND database_occupancy OPERATOR(pg_catalog.=) 0::pg_catalog.int8 THEN 'EMPTY'
    WHEN foreign_relations OPERATOR(pg_catalog.>) 0::pg_catalog.int8 OR functions OPERATOR(pg_catalog.>) 0::pg_catalog.int8
         OR database_occupancy OPERATOR(pg_catalog.>) 0::pg_catalog.int8 THEN 'REFUSE_UNTRACKED_OBJECTS'
    ELSE 'RUNTIME_ONLY'
END FROM facts
$fcbq$ INTO s;
    IF s OPERATOR(pg_catalog.=) 'TRACKED'::pg_catalog.text THEN
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
got AS (SELECT m.version, m.name, m.sha256 FROM public.schema_migrations m),
n AS (SELECT pg_catalog.count(*) AS rows, pg_catalog.count(DISTINCT got.version) AS versions FROM got)
SELECT CASE
    WHEN EXISTS (SELECT 1 FROM got WHERE got.version OPERATOR(pg_catalog.=) 8
                 AND got.name OPERATOR(pg_catalog.=) '0008_signer_ledger_anchor.sql'::pg_catalog.text)
        THEN 'REFUSE_PRE_SPLIT_SIGNER_0008'
    WHEN (SELECT n.rows FROM n) OPERATOR(pg_catalog.=) 7::pg_catalog.int8
         AND (SELECT n.versions FROM n) OPERATOR(pg_catalog.=) 7::pg_catalog.int8 AND NOT EXISTS (
            SELECT got.version, got.name, got.sha256 FROM got
            EXCEPT SELECT e.version, e.name, e.sha256 FROM expected e WHERE e.version OPERATOR(pg_catalog.<=) 7)
        THEN 'BRIDGE_0001_0007'
    WHEN (SELECT n.rows FROM n) OPERATOR(pg_catalog.=) 8::pg_catalog.int8
         AND (SELECT n.versions FROM n) OPERATOR(pg_catalog.=) 8::pg_catalog.int8 AND NOT EXISTS (
            SELECT got.version, got.name, got.sha256 FROM got EXCEPT SELECT e.version, e.name, e.sha256 FROM expected e)
        THEN 'BRIDGE_0001_0008'
    ELSE 'REFUSE_PARTIAL_OR_DRIFT'
END
$fcbq$ INTO s;
    END IF;
    IF s IS NULL OR s OPERATOR(pg_catalog.<>) 'BRIDGE_0001_0008'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_RESTORE_POST_STATE %', s;
    END IF;
    EXECUTE $fcbq$
WITH s AS (SELECT 'public'::pg_catalog.text AS n),
me AS (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) CURRENT_USER),
c AS (SELECT cl.* FROM pg_catalog.pg_class cl JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) cl.relnamespace, s
      WHERE n.nspname OPERATOR(pg_catalog.=) s.n::pg_catalog.name),
lines(x) AS (
    SELECT 'rel '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) c.relkind::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) c.relpersistence::pg_catalog.text OPERATOR(pg_catalog.||) ' acl='::pg_catalog.text
        OPERATOR(pg_catalog.||) CASE WHEN (c.relacl IS NULL OR (c.relname::pg_catalog.text OPERATOR(pg_catalog.=) ANY (ARRAY['api_football_quota_counters', 'api_football_quota_leases', 'api_football_quota_attempts']::pg_catalog.text[]) AND (SELECT pg_catalog.string_agg(z.e, ','::pg_catalog.text ORDER BY z.e) FROM (SELECT x.grantor::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.grantee::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e FROM pg_catalog.aclexplode(c.relacl) x) z) OPERATOR(pg_catalog.=) (SELECT pg_catalog.string_agg(z.e, ','::pg_catalog.text ORDER BY z.e) FROM (SELECT x.grantor::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.grantee::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e FROM pg_catalog.aclexplode(pg_catalog.acldefault('r'::pg_catalog."char", c.relowner)) x UNION ALL SELECT c.relowner::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) w.oid::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) pv.pv OPERATOR(pg_catalog.||) '/false'::pg_catalog.text FROM (SELECT r.oid AS oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb AND NOT r.rolbypassrls AND NOT r.rolreplication AND NOT r.rolinherit AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member OPERATOR(pg_catalog.=) r.oid)) w, pg_catalog.unnest(CASE WHEN c.relname::pg_catalog.text OPERATOR(pg_catalog.=) 'api_football_quota_counters'::pg_catalog.text THEN ARRAY['INSERT', 'SELECT']::pg_catalog.text[] ELSE ARRAY['DELETE', 'INSERT', 'SELECT']::pg_catalog.text[] END) pv) z))) THEN '-'::pg_catalog.text ELSE c.relacl::pg_catalog.text END
        OPERATOR(pg_catalog.||) ' opt='::pg_catalog.text OPERATOR(pg_catalog.||) COALESCE(c.reloptions::pg_catalog.text, '-'::pg_catalog.text)
        OPERATOR(pg_catalog.||) ' rls='::pg_catalog.text OPERATOR(pg_catalog.||) c.relrowsecurity::pg_catalog.text
        OPERATOR(pg_catalog.||) ' ri='::pg_catalog.text OPERATOR(pg_catalog.||) c.relreplident::pg_catalog.text
        OPERATOR(pg_catalog.||) ' own='::pg_catalog.text
        OPERATOR(pg_catalog.||) (c.relowner OPERATOR(pg_catalog.=) (SELECT me.oid FROM me))::pg_catalog.text
    FROM c WHERE c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'v', 'm', 'S', 'f', 'i', 'I', 'c']::pg_catalog."char"[])
    UNION ALL
    SELECT 'col '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) '.'::pg_catalog.text
        OPERATOR(pg_catalog.||) a.attname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) pg_catalog.format_type(a.atttypid, a.atttypmod)
        OPERATOR(pg_catalog.||) ' coll='::pg_catalog.text
        OPERATOR(pg_catalog.||) COALESCE((SELECT cn.nspname::pg_catalog.text OPERATOR(pg_catalog.||) '.'::pg_catalog.text
                                                 OPERATOR(pg_catalog.||) co.collname::pg_catalog.text
                                          FROM pg_catalog.pg_collation co
                                          JOIN pg_catalog.pg_namespace cn ON cn.oid OPERATOR(pg_catalog.=) co.collnamespace
                                          WHERE co.oid OPERATOR(pg_catalog.=) a.attcollation), '-'::pg_catalog.text)
        OPERATOR(pg_catalog.||) ' nn='::pg_catalog.text OPERATOR(pg_catalog.||) a.attnotnull::pg_catalog.text
        OPERATOR(pg_catalog.||) ' def='::pg_catalog.text
        OPERATOR(pg_catalog.||) COALESCE(pg_catalog.pg_get_expr(d.adbin, d.adrelid), '-'::pg_catalog.text)
        OPERATOR(pg_catalog.||) ' acl='::pg_catalog.text OPERATOR(pg_catalog.||) CASE WHEN (a.attacl IS NULL OR (c.relname::pg_catalog.text OPERATOR(pg_catalog.=) 'api_football_quota_counters'::pg_catalog.text AND a.attname OPERATOR(pg_catalog.=) 'used'::pg_catalog.name AND (SELECT pg_catalog.string_agg(z.e, ','::pg_catalog.text ORDER BY z.e) FROM (SELECT x.grantor::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.grantee::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.privilege_type::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) x.is_grantable::pg_catalog.text AS e FROM pg_catalog.aclexplode(a.attacl) x) z) OPERATOR(pg_catalog.=) (SELECT (SELECT o.relowner FROM pg_catalog.pg_class o WHERE o.oid OPERATOR(pg_catalog.=) a.attrelid)::pg_catalog.text OPERATOR(pg_catalog.||) '/'::pg_catalog.text OPERATOR(pg_catalog.||) w.oid::pg_catalog.text OPERATOR(pg_catalog.||) '/UPDATE/false'::pg_catalog.text FROM (SELECT r.oid AS oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb AND NOT r.rolbypassrls AND NOT r.rolreplication AND NOT r.rolinherit AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member OPERATOR(pg_catalog.=) r.oid)) w))) THEN '-'::pg_catalog.text ELSE a.attacl::pg_catalog.text END
        OPERATOR(pg_catalog.||) ' id='::pg_catalog.text OPERATOR(pg_catalog.||) a.attidentity::pg_catalog.text
        OPERATOR(pg_catalog.||) ' gen='::pg_catalog.text OPERATOR(pg_catalog.||) a.attgenerated::pg_catalog.text
    FROM c JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) c.oid
        AND a.attnum OPERATOR(pg_catalog.>) 0::pg_catalog.int2 AND NOT a.attisdropped
    LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid OPERATOR(pg_catalog.=) c.oid AND d.adnum OPERATOR(pg_catalog.=) a.attnum
    WHERE c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'v', 'm', 'f']::pg_catalog."char"[])
    UNION ALL
    SELECT 'con '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) k.conname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) k.contype::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) pg_catalog.pg_get_constraintdef(k.oid)
        OPERATOR(pg_catalog.||) ' valid='::pg_catalog.text OPERATOR(pg_catalog.||) k.convalidated::pg_catalog.text
    FROM c JOIN pg_catalog.pg_constraint k ON k.conrelid OPERATOR(pg_catalog.=) c.oid
    UNION ALL
    SELECT 'idx '::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.pg_get_indexdef(i.indexrelid)
        OPERATOR(pg_catalog.||) ' valid='::pg_catalog.text OPERATOR(pg_catalog.||) i.indisvalid::pg_catalog.text
        OPERATOR(pg_catalog.||) ' ready='::pg_catalog.text OPERATOR(pg_catalog.||) i.indisready::pg_catalog.text
    FROM c JOIN pg_catalog.pg_index i ON i.indrelid OPERATOR(pg_catalog.=) c.oid
    UNION ALL
    SELECT 'trg '::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.pg_get_triggerdef(t.oid)
        OPERATOR(pg_catalog.||) ' enabled='::pg_catalog.text OPERATOR(pg_catalog.||) t.tgenabled::pg_catalog.text
    FROM c JOIN pg_catalog.pg_trigger t ON t.tgrelid OPERATOR(pg_catalog.=) c.oid WHERE NOT t.tgisinternal
    UNION ALL
    SELECT 'rul '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) r.rulename::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) pg_catalog.pg_get_ruledef(r.oid)
        OPERATOR(pg_catalog.||) ' enabled='::pg_catalog.text OPERATOR(pg_catalog.||) r.ev_enabled::pg_catalog.text
    FROM c JOIN pg_catalog.pg_rewrite r ON r.ev_class OPERATOR(pg_catalog.=) c.oid
    UNION ALL
    SELECT 'pol '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) p.polname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) p.polcmd::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) COALESCE(pg_catalog.pg_get_expr(p.polqual, p.polrelid), '-'::pg_catalog.text)
    FROM c JOIN pg_catalog.pg_policy p ON p.polrelid OPERATOR(pg_catalog.=) c.oid
    UNION ALL
    SELECT 'fun '::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.pg_get_functiondef(p.oid)
        OPERATOR(pg_catalog.||) ' acl='::pg_catalog.text OPERATOR(pg_catalog.||) COALESCE(p.proacl::pg_catalog.text, '-'::pg_catalog.text)
        OPERATOR(pg_catalog.||) ' own='::pg_catalog.text
        OPERATOR(pg_catalog.||) (p.proowner OPERATOR(pg_catalog.=) (SELECT me.oid FROM me))::pg_catalog.text
    FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace, s
    WHERE n.nspname OPERATOR(pg_catalog.=) s.n::pg_catalog.name
      AND p.prokind OPERATOR(pg_catalog.=) ANY (ARRAY['f', 'p']::pg_catalog."char"[])
    UNION ALL
    SELECT 'typ '::pg_catalog.text OPERATOR(pg_catalog.||) t.typname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
        OPERATOR(pg_catalog.||) t.typtype::pg_catalog.text
    FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) t.typnamespace, s
    WHERE n.nspname OPERATOR(pg_catalog.=) s.n::pg_catalog.name
      AND t.typtype OPERATOR(pg_catalog.=) ANY (ARRAY['e', 'd', 'r', 'm', 'b']::pg_catalog."char"[])
)
SELECT pg_catalog.md5(pg_catalog.string_agg(
    pg_catalog.replace(l.x, (SELECT s.n FROM s) OPERATOR(pg_catalog.||) '.'::pg_catalog.text, ''::pg_catalog.text), E'\n'::pg_catalog.text
    ORDER BY pg_catalog.replace(l.x, (SELECT s.n FROM s) OPERATOR(pg_catalog.||) '.'::pg_catalog.text, ''::pg_catalog.text)))
FROM lines l
$fcbq$ INTO f;
    IF f IS NULL OR f OPERATOR(pg_catalog.<>) pg_catalog.current_setting('fcb.expected_fingerprint') THEN
        RAISE EXCEPTION 'FCB_RESTORE_FINGERPRINT_MISMATCH %', f;
    END IF;
    IF prior IS NULL OR prior OPERATOR(pg_catalog.<>) ALL (ARRAY['EMPTY', 'RUNTIME_ONLY', 'BRIDGE_0001_0007', 'BRIDGE_0001_0008']::pg_catalog.text[]) THEN
        RAISE EXCEPTION 'FCB_RESTORE_PRIOR_STATE_UNKNOWN';
    END IF;
    IF prior OPERATOR(pg_catalog.=) 'EMPTY'::pg_catalog.text THEN
        SELECT (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.relnamespace WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.pronamespace WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_type x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.typnamespace WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_constraint x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.connamespace WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_collation x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.collnamespace WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_operator x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.oprnamespace WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_opclass x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.opcnamespace WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_opfamily x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.opfnamespace WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_conversion x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.connamespace WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_config x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.cfgnamespace WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_ts_dict x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.dictnamespace WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_extension x WHERE x.extname OPERATOR(pg_catalog.<>) 'plpgsql'::pg_catalog.name)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_largeobject_metadata)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_publication)
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_subscription x WHERE x.subdbid OPERATOR(pg_catalog.=) (SELECT d.oid FROM pg_catalog.pg_database d WHERE d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()))
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_prepared_xacts x WHERE x.database OPERATOR(pg_catalog.=) pg_catalog.current_database())
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_replication_slots x WHERE x.database OPERATOR(pg_catalog.=) pg_catalog.current_database())
            OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_default_acl)
        INTO foreign_rows;
        IF foreign_rows IS NULL OR foreign_rows OPERATOR(pg_catalog.<>) 0::pg_catalog.int8 THEN
            RAISE EXCEPTION 'FCB_RESTORE_FOREIGN_OBJECTS_AFTER_EMPTY';
        END IF;
    END IF;
END $fcbpost$;
