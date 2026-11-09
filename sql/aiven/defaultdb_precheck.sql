-- FCB-WP-005: read-only restore precheck for the schema `public` (the only schema it accepts).
-- Prints exactly one word: EMPTY or RUNTIME_ONLY may proceed, TRACKED is resolved by
-- defaultdb_precheck_tracked.sql, every REFUSE_* state stops the restore (fail closed).
-- Runtime tables (the ones the running code creates itself) are checked in EVERY state, tracked
-- or not, because CREATE TABLE IF NOT EXISTS in 0007/0008 would skip and then certify them:
-- each must be EXACTLY as the code creates it (columns, types, collations, NOT NULL, defaults,
-- constraints; indexes a subset of the expected ones), a plain permanent heap table owned by
-- the restoring role, default privileges, no reloptions, default replica identity, no rule,
-- trigger, policy, RLS, inheritance, identity or generated column.
-- In EVERY state (tracked included, audit F2): functions must be the four pinned bridge trigger
-- functions (signature, language, SECURITY, config and md5 of the body), triggers the nine
-- pinned bridge triggers (enabled, no arguments/columns/WHEN), and there may be no type that is
-- not the row/array type of a relation, no rule, no policy, no RLS flag and no inheritance on any
-- relation of public. No schema besides the system ones may exist (anchored temp-schema regex;
-- Aiven's `_avnadmin_monitor` only in Aiven's real shape, audit F1), and no database/role
-- setting may apply to this database or role (search_path or anything else, audit F5).
-- EMPTY additionally requires no type, extension (except plpgsql), large object, subscription,
-- prepared transaction or replication slot of this database (audit F6).
-- pg_catalog must come first in the search path and the current schema must be public, and the
-- schema may hold no operator, operator class/family, collation, conversion, text-search
-- object or extension, so nothing can shadow a built-in. Every catalog relation, function and
-- operator below is schema-qualified (audit F3): objects planted in public cannot change how
-- this query resolves, even under a search_path changed mid-transaction. Database-wide default
-- privileges and enabled event triggers are refused: they would act on the restore's own DDL.
-- The signature below was taken from a clean runtime creation; tests keep it in sync.
-- No data row is read.
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
    -- Every schema that is not a system schema: anchored temp-schema regex (LIKE 'pg_temp%'
    -- also matched 'pgxtemp'); `_avnadmin_monitor` is excluded only in Aiven's real shape: owned
    -- by Aiven's `_avnadmin_monitor` role (or a superuser), not by the session user, who is
    -- neither a member of its owner nor allowed to CREATE in it (as pg18_occupancy.sql).
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
        -- audit F2: functions outside the pinned bridge set, in every state
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
        -- audit F2: every type must be the row type of a relation of public or its array type
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_type t JOIN ns ON ns.oid OPERATOR(pg_catalog.=) t.typnamespace
            WHERE NOT EXISTS (SELECT 1 FROM allrel c WHERE c.reltype OPERATOR(pg_catalog.<>) 0::pg_catalog.oid
                AND (c.reltype OPERATOR(pg_catalog.=) t.oid
                     OR EXISTS (SELECT 1 FROM pg_catalog.pg_type e WHERE e.oid OPERATOR(pg_catalog.=) c.reltype
                                AND e.typarray OPERATOR(pg_catalog.=) t.oid)))) AS foreign_types,
        -- audit F2: triggers outside the pinned bridge set, on ANY relation of public
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
        -- audit F2: no rule, policy, RLS flag or inheritance on ANY relation of public
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_rewrite rw JOIN allrel c ON c.oid OPERATOR(pg_catalog.=) rw.ev_class)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_policy pol JOIN allrel c ON c.oid OPERATOR(pg_catalog.=) pol.polrelid)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM allrel c WHERE c.relrowsecurity OR c.relforcerowsecurity)
        OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_inherits ih
            WHERE EXISTS (SELECT 1 FROM allrel c WHERE c.oid OPERATOR(pg_catalog.=) ih.inhrelid OR c.oid OPERATOR(pg_catalog.=) ih.inhparent))
          AS relation_hooks,
        -- columns and constraints must match exactly; indexes may only be a subset
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
            OR p.relreplident OPERATOR(pg_catalog.<>) 'd'::pg_catalog."char" OR p.relacl IS NOT NULL
            OR p.relowner OPERATOR(pg_catalog.<>) (SELECT me.oid FROM me)
            OR p.relrowsecurity
            OR p.relam OPERATOR(pg_catalog.<>) (SELECT am.oid FROM pg_catalog.pg_am am WHERE am.amname OPERATOR(pg_catalog.=) 'heap'::pg_catalog.name)) AS table_props,
        (SELECT pg_catalog.count(*) FROM present p JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) p.oid
            AND a.attnum OPERATOR(pg_catalog.>) 0::pg_catalog.int2
            WHERE a.attacl IS NOT NULL OR a.attoptions IS NOT NULL
               OR a.attidentity OPERATOR(pg_catalog.<>) ''::pg_catalog."char"
               OR a.attgenerated OPERATOR(pg_catalog.<>) ''::pg_catalog."char") AS column_props,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_policy pol JOIN present p ON p.oid OPERATOR(pg_catalog.=) pol.polrelid) AS policies,
        -- PG18: NOT NULL constraints may exist NOT VALID while attnotnull stays true
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_constraint k JOIN present p ON p.oid OPERATOR(pg_catalog.=) k.conrelid
            WHERE NOT k.convalidated) AS unvalidated,
        -- objects that can change how SQL resolves or behaves, never created by 0001-0008
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
        -- aggregates, window functions and procedures can shadow built-ins by argument type even
        -- with pg_catalog first; 0001-0008 create plain functions only
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace
            WHERE p.prokind OPERATOR(pg_catalog.<>) 'f'::pg_catalog."char") AS non_function_routines,
        -- database-wide hazards: they would act on the restore's own DDL, then be certified
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_publication) AS publications,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_default_acl) AS default_acls,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_event_trigger et
            WHERE et.evtenabled OPERATOR(pg_catalog.<>) 'D'::pg_catalog."char") AS event_triggers,
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_db_role_setting rs
            WHERE (rs.setdatabase OPERATOR(pg_catalog.=) (SELECT db.oid FROM db) OR rs.setdatabase OPERATOR(pg_catalog.=) 0::pg_catalog.oid)
              AND (rs.setrole OPERATOR(pg_catalog.=) 0::pg_catalog.oid OR rs.setrole OPERATOR(pg_catalog.=) (SELECT me.oid FROM me))
              AND pg_catalog.array_to_string(rs.setconfig, ','::pg_catalog.text) OPERATOR(pg_catalog.~~) '%search_path%'::pg_catalog.text) AS search_path_settings,
        -- audit F5: any other setting of this database (any role), of this role or of every role
        -- (e.g. `role`, `session_authorization`-like effects, check_function_bodies)
        (SELECT pg_catalog.count(*) FROM pg_catalog.pg_db_role_setting rs, pg_catalog.unnest(rs.setconfig) AS cfg(c)
            WHERE (rs.setdatabase OPERATOR(pg_catalog.=) (SELECT db.oid FROM db)
                   OR (rs.setdatabase OPERATOR(pg_catalog.=) 0::pg_catalog.oid
                       AND (rs.setrole OPERATOR(pg_catalog.=) 0::pg_catalog.oid OR rs.setrole OPERATOR(pg_catalog.=) (SELECT me.oid FROM me)
                            OR rs.setrole OPERATOR(pg_catalog.=) (SELECT sess.oid FROM sess))))
              AND cfg.c OPERATOR(pg_catalog.!~) '^search_path='::pg_catalog.text) AS other_settings,
        -- audit F6: database-wide occupancy that an EMPTY (or runtime-only) database cannot have
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
        -- audit F4: catalog lookups, no to_regclass(quote_ident(...)) resolution
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
END FROM facts;
