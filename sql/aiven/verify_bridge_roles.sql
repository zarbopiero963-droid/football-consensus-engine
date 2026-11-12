-- FCB-WP-009: read-only check of the bridge privilege model in schema public. Prints one line
-- (and sets the psql variable :fcb_roles_state for sql/aiven/assert_bridge_roles.sql):
--   ROLES_ABSENT              none of fcb_migrator, fcb_app, fcb_signer exists
--   HARDENED                  exactly the WP-009 model below
--   NOT_HARDENED:<codes>      anything else; <codes> names every failing check
-- No secret, no row content, no password hash is read or printed.
--
-- The WP-009 model:
--   roles      fcb_migrator LOGIN, fcb_app LOGIN, fcb_signer NOLOGIN; none SUPERUSER, CREATEDB,
--              CREATEROLE, REPLICATION or BYPASSRLS; none is a member of any role;
--              the only member of fcb_migrator is the session (administrative) identity, with
--              INHERIT (rollback: the previous runtime DSN keeps working); fcb_app and fcb_signer
--              have no member except their creator's implicit ADMIN-only grant;
--              exact membership inventory (pg_auth_members rows involving a bridge role):
--                fcb_app      <- session  ADMIN only            grantor bootstrap superuser (implicit)
--                fcb_signer   <- session  ADMIN only            grantor bootstrap superuser (implicit)
--                fcb_migrator <- session  ADMIN only            grantor bootstrap superuser (implicit)
--                fcb_migrator <- session  INHERIT + SET, no ADMIN, grantor session (explicit)
--              and no bridge role can reach (MEMBER) another bridge role or the session;
--   ownership  every relation, routine and type in public is owned by fcb_migrator;
--   tables     the only non-owner privileges in public are the runtime allowlist of fcb_app
--              and (PATH A2) the exact quota-coordinator grant of a plain fcb_cert_runner;
--              (finalize_bridge_privileges.sql); no column privilege; no PUBLIC or signer grant;
--   routines   no non-owner EXECUTE (PUBLIC revoked);
--   schema     public: fcb_migrator USAGE+CREATE, fcb_app USAGE, nothing for PUBLIC/fcb_signer
--              (grants held by the schema owner or the session identity are not judged);
--   database   no bridge role may CREATE (schemas) in the database;
--   settings   no per-role setting (ALTER ROLE ... SET) for any bridge role;
--   code       the routines in public are exactly the 4 pinned bridge functions (name, arguments,
--              return type, language, SECURITY INVOKER, no SET, md5 of the body) and the
--              non-internal triggers exactly the 9 pinned bridge triggers (same lists as
--              defaultdb_precheck.sql): fcb_migrator cannot plant code that would run as the
--              administrator when it writes to the bridge (rollback DSN); no rule, policy, RLS,
--              inheritance, view, matview, foreign or partitioned table, enabled event trigger;
--              and no type (other than the tables' row/array types), operator, operator class or
--              family, collation, conversion, text-search object, extension or cast in public;
--   defaults   no default privilege grants anything to a role other than its owner, and
--              fcb_migrator's GLOBAL functions default revokes PUBLIC EXECUTE (an IN SCHEMA
--              revoke cannot remove the built-in PUBLIC EXECUTE).
-- Every catalog relation, function and operator is schema-qualified (audit F3).
WITH
names(rolname) AS (SELECT pg_catalog.unnest(ARRAY['fcb_migrator', 'fcb_app', 'fcb_signer']::pg_catalog.name[])),
rl AS (SELECT r.* FROM pg_catalog.pg_roles r JOIN names ON names.rolname OPERATOR(pg_catalog.=) r.rolname),
mig AS (SELECT rl.oid FROM rl WHERE rl.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name),
app AS (SELECT rl.oid FROM rl WHERE rl.rolname OPERATOR(pg_catalog.=) 'fcb_app'::pg_catalog.name),
sgn AS (SELECT rl.oid FROM rl WHERE rl.rolname OPERATOR(pg_catalog.=) 'fcb_signer'::pg_catalog.name),
sess AS (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER),
-- Phase: fcb_migrator LOGIN means phase 2 (it owns the bridge); NOLOGIN means phase 1 (the
-- session identity still owns it). The expected owner and schema rights follow the phase.
phase AS (SELECT COALESCE((SELECT r.rolcanlogin FROM rl r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_migrator'::pg_catalog.name), false) AS two),
owner_oid AS (SELECT CASE WHEN (SELECT phase.two FROM phase) THEN (SELECT mig.oid FROM mig) ELSE (SELECT sess.oid FROM sess) END AS oid),
ns AS (SELECT n.oid, n.nspowner, n.nspacl FROM pg_catalog.pg_namespace n WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name),
rels AS (SELECT c.oid, c.relname, c.relkind, c.relowner, c.relacl FROM pg_catalog.pg_class c JOIN ns ON ns.oid OPERATOR(pg_catalog.=) c.relnamespace),
rel_grants AS (
    SELECT c.relname::pg_catalog.text AS relname, a.grantee, a.privilege_type::pg_catalog.text AS priv
    FROM rels c, LATERAL pg_catalog.aclexplode(COALESCE(c.relacl, pg_catalog.acldefault(
        CASE WHEN c.relkind OPERATOR(pg_catalog.=) 'S'::pg_catalog."char" THEN 's'::pg_catalog."char" ELSE 'r'::pg_catalog."char" END,
        c.relowner))) a
    WHERE c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'v', 'm', 'S', 'f']::pg_catalog."char"[])
      AND a.grantee OPERATOR(pg_catalog.<>) c.relowner
),
-- Bridge state (WP-009 compatibility, owner GO): which of the three admitted bridge states the
-- catalog holds, from the relations present: 8 = 0001-0008 (no 0009/0010 table), 9 = 0001-0009
-- (both 0009 tables, no 0010 table), 10 = 0001-0010 (both 0009 tables and all five 0010 tables).
-- Anything else (0010 without 0009, a partial 0009 or 0010) is 0 and fails 'bridge_state'. The
-- exact objects of the detected state are then required by every check below (relations' grants,
-- column grants, routines, triggers, expression functions); the workflow also requires the
-- schema_migrations rows (read AS fcb_app) and the pinned structure fingerprint of that state.
bridge_presence AS (
    SELECT (SELECT pg_catalog.count(*) FROM rels c WHERE c.relkind OPERATOR(pg_catalog.=) 'r'::pg_catalog."char"
              AND c.relname OPERATOR(pg_catalog.=) ANY (ARRAY['oauth_throttle_buckets', 'oauth_throttle_unlock_audit']::pg_catalog.name[])) AS v9,
           (SELECT pg_catalog.count(*) FROM rels c WHERE c.relkind OPERATOR(pg_catalog.=) 'r'::pg_catalog."char"
              AND c.relname OPERATOR(pg_catalog.=) ANY (ARRAY['api_football_cert_plans', 'api_football_cert_plan_usage', 'api_football_cert_campaigns',
                                           'api_football_cert_campaign_attempts', 'api_football_cert_campaign_outcomes']::pg_catalog.name[])) AS v10
),
bridge_state AS (
    SELECT CASE WHEN b.v9 OPERATOR(pg_catalog.=) 0 AND b.v10 OPERATOR(pg_catalog.=) 0 THEN 8
                WHEN b.v9 OPERATOR(pg_catalog.=) 2 AND b.v10 OPERATOR(pg_catalog.=) 0 THEN 9
                WHEN b.v9 OPERATOR(pg_catalog.=) 2 AND b.v10 OPERATOR(pg_catalog.=) 5 THEN 10
                ELSE 0 END AS v
    FROM bridge_presence b
),
runtime_tables_by_state(relname, privs, since) AS (VALUES
    -- 0009 (#198): the persistent OAuth throttle; the unlock audit gets no runtime privilege.
    ('oauth_throttle_buckets'::pg_catalog.text, ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']::pg_catalog.text[], 9),
    -- 0010 (#199): the certification ledger: SELECT on the tables (INSERT is column-level, below);
    -- nothing at all on the plan counter (api_football_cert_plan_usage).
    ('api_football_cert_plans', ARRAY['SELECT'], 10),
    ('api_football_cert_campaigns', ARRAY['SELECT'], 10),
    ('api_football_cert_campaign_attempts', ARRAY['SELECT'], 10),
    ('api_football_cert_campaign_outcomes', ARRAY['SELECT'], 10)
),
expected_column_grants(relname, attname, priv, since) AS (VALUES
    ('api_football_cert_campaigns'::pg_catalog.text, 'campaign_id'::pg_catalog.text, 'INSERT'::pg_catalog.text, 10),
    ('api_football_cert_campaigns', 'plan_sha256', 'INSERT', 10),
    ('api_football_cert_campaign_attempts', 'campaign_id', 'INSERT', 10),
    ('api_football_cert_campaign_attempts', 'call_id', 'INSERT', 10),
    ('api_football_cert_campaign_outcomes', 'campaign_id', 'INSERT', 10),
    ('api_football_cert_campaign_outcomes', 'seq', 'INSERT', 10),
    ('api_football_cert_campaign_outcomes', 'outcome', 'INSERT', 10),
    ('api_football_cert_campaign_outcomes', 'http_status', 'INSERT', 10)
),
column_grants AS (
    SELECT c.relname::pg_catalog.text AS relname, a.attname::pg_catalog.text AS attname, x.grantee,
           x.privilege_type::pg_catalog.text AS priv
    FROM rels c JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) c.oid,
         LATERAL pg_catalog.aclexplode(a.attacl) x
    WHERE a.attacl IS NOT NULL
),
runtime_tables(relname, privs) AS (
    SELECT t.relname, t.privs FROM runtime_tables_by_state t, bridge_state b WHERE t.since OPERATOR(pg_catalog.<=) b.v
    UNION ALL SELECT * FROM (VALUES
    ('schema_migrations'::pg_catalog.text, ARRAY['SELECT']::pg_catalog.text[]),
    ('oauth_authorization_codes', ARRAY['SELECT', 'INSERT', 'UPDATE']),
    ('oauth_access_tokens', ARRAY['SELECT', 'INSERT']),
    ('oauth_refresh_tokens', ARRAY['SELECT', 'INSERT', 'UPDATE']),
    ('oauth_claude_dcr_clients', ARRAY['SELECT', 'INSERT', 'DELETE']),
    ('browser_session_leases', ARRAY['SELECT', 'INSERT', 'UPDATE']),
    ('api_football_quota_counters', ARRAY['SELECT', 'INSERT', 'UPDATE']),
    ('api_football_quota_leases', ARRAY['SELECT', 'INSERT', 'DELETE']),
    ('api_football_quota_attempts', ARRAY['SELECT', 'INSERT', 'DELETE']),
    ('sxbet_rate_attempts', ARRAY['SELECT', 'INSERT', 'DELETE']),
    ('sxbet_rate_cooldowns', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'])
    ) AS base(relname, privs)
),
-- PATH A2 (owner GO): the dedicated certification runner fcb_cert_runner, when it exists as a PLAIN
-- role (no attribute, member of nothing), holds EXACTLY the shared quota coordinator grant of
-- sql/aiven/apifootball_cert_path_a_roles.sql and nothing else; any other privilege of it, or the
-- same privileges held by a non-plain fcb_cert_runner, stays unexpected.
cert_runner AS (
    SELECT r.oid FROM pg_catalog.pg_roles r
    WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name
      AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb AND NOT r.rolbypassrls
      AND NOT r.rolreplication AND NOT r.rolinherit
      AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member OPERATOR(pg_catalog.=) r.oid)
),
cert_runner_grants(relname, priv) AS (VALUES
    ('api_football_quota_counters'::pg_catalog.text, 'SELECT'::pg_catalog.text),
    ('api_football_quota_counters', 'INSERT'),
    ('api_football_quota_leases', 'SELECT'),
    ('api_football_quota_leases', 'INSERT'),
    ('api_football_quota_leases', 'DELETE'),
    ('api_football_quota_attempts', 'SELECT'),
    ('api_football_quota_attempts', 'INSERT'),
    ('api_football_quota_attempts', 'DELETE')
),
cert_runner_column_grants(relname, attname, priv) AS (VALUES
    ('api_football_quota_counters'::pg_catalog.text, 'used'::pg_catalog.text, 'UPDATE'::pg_catalog.text)
),
expected_grants AS (
    SELECT t.relname, (SELECT app.oid FROM app) AS grantee, p.priv
    FROM runtime_tables t, LATERAL pg_catalog.unnest(t.privs) AS p(priv)
    UNION ALL SELECT g.relname, w.oid, g.priv FROM cert_runner_grants g, cert_runner w
),
schema_grants AS (
    SELECT a.grantee, a.privilege_type::pg_catalog.text AS priv
    FROM ns, LATERAL pg_catalog.aclexplode(COALESCE(ns.nspacl, pg_catalog.acldefault('n'::pg_catalog."char", ns.nspowner))) a
    WHERE a.grantee OPERATOR(pg_catalog.<>) ns.nspowner AND a.grantee OPERATOR(pg_catalog.<>) (SELECT sess.oid FROM sess)
),
expected_schema_grants(grantee, priv) AS (
    SELECT (SELECT app.oid FROM app), 'USAGE'::pg_catalog.text
    UNION ALL SELECT (SELECT mig.oid FROM mig), 'USAGE'::pg_catalog.text WHERE (SELECT phase.two FROM phase)
    UNION ALL SELECT (SELECT mig.oid FROM mig), 'CREATE'::pg_catalog.text WHERE (SELECT phase.two FROM phase)
),
inventory AS (
    SELECT m.roleid, m.member, m.admin_option, m.inherit_option, m.set_option, m.grantor
    FROM pg_catalog.pg_auth_members m
    WHERE m.roleid OPERATOR(pg_catalog.=) ANY (ARRAY(SELECT rl.oid FROM rl))
       OR m.member OPERATOR(pg_catalog.=) ANY (ARRAY(SELECT rl.oid FROM rl))
),
expected_inventory(roleid, member, admin_option, inherit_option, set_option, grantor) AS (
    SELECT (SELECT app.oid FROM app), (SELECT sess.oid FROM sess), true, false, false, 10::pg_catalog.oid
    UNION ALL SELECT (SELECT sgn.oid FROM sgn), (SELECT sess.oid FROM sess), true, false, false, 10::pg_catalog.oid
    UNION ALL SELECT (SELECT mig.oid FROM mig), (SELECT sess.oid FROM sess), true, false, false, 10::pg_catalog.oid
),
-- R1: every role of the cluster (superusers aside: they bypass every check) against each bridge
-- role: only the session identity may be a MEMBER (its automatic ADMIN-only grant), and nobody
-- may inherit a bridge role's privileges or SET ROLE to it.
reach AS (
    SELECT r.oid AS member, t.oid AS target,
           pg_catalog.pg_has_role(r.oid, t.oid, 'MEMBER') AS m,
           pg_catalog.pg_has_role(r.oid, t.oid, 'USAGE') AS u,
           pg_catalog.pg_has_role(r.oid, t.oid, 'SET') AS s
    FROM pg_catalog.pg_roles r, rl t
    WHERE NOT r.rolsuper AND r.oid OPERATOR(pg_catalog.<>) t.oid
),
-- Function calls inside stored expressions of public (defaults, generated columns, CHECK
-- constraints, index expressions and predicates): only these pg_catalog functions, the ones the
-- bridge 0001-0008 itself uses. A default or CHECK calling e.g. set_config would run with the
-- rights of whoever writes the row.
allowed_expr_functions(sig) AS (VALUES
    ('numeric(integer)'), ('numeric(numeric, integer)'), ('btrim(text)'), ('clock_timestamp()'),
    ('jsonb_typeof(jsonb)'), ('length(text)'), ('nextval(regclass)'), ('now()'),
    ('int4gt(integer, integer)'), ('int4le(integer, integer)'), ('jsonb_ne(jsonb, jsonb)'),
    ('texteq(text, text)'), ('textlike(text, text)'), ('textne(text, text)'), ('textregexeq(text, text)'),
    ('timestamptz_gt(timestamp with time zone, timestamp with time zone)')
),
-- 0010 (#199) adds exactly these in its CHECK constraints and defaults (budget ranges, the
-- outcome/status coherence, github_run_id > 0, approved_role DEFAULT session_user).
allowed_expr_functions_0010(sig) AS (VALUES
    ('booleq(boolean, boolean)'), ('int4ge(integer, integer)'), ('int84gt(bigint, integer)'), ('text(name)')
),
allowed_expr_functions_state(sig) AS (
    SELECT a.sig FROM allowed_expr_functions a
    UNION ALL SELECT a.sig FROM allowed_expr_functions_0010 a, bridge_state b WHERE b.v OPERATOR(pg_catalog.>=) 10
),
expr_trees(tree) AS (
    SELECT d.adbin::pg_catalog.text FROM pg_catalog.pg_attrdef d JOIN rels c ON c.oid OPERATOR(pg_catalog.=) d.adrelid
    UNION ALL SELECT k.conbin::pg_catalog.text FROM pg_catalog.pg_constraint k JOIN rels c ON c.oid OPERATOR(pg_catalog.=) k.conrelid
        WHERE k.conbin IS NOT NULL
    UNION ALL SELECT i.indexprs::pg_catalog.text FROM pg_catalog.pg_index i JOIN rels c ON c.oid OPERATOR(pg_catalog.=) i.indrelid
        WHERE i.indexprs IS NOT NULL
    UNION ALL SELECT i.indpred::pg_catalog.text FROM pg_catalog.pg_index i JOIN rels c ON c.oid OPERATOR(pg_catalog.=) i.indrelid
        WHERE i.indpred IS NOT NULL
),
expr_function_oids(oid) AS (
    SELECT (m.v[2])::pg_catalog.oid FROM expr_trees e,
        LATERAL pg_catalog.regexp_matches(e.tree, ':(funcid|opfuncid|hashfuncid|negfuncid|aggfnoid|winfnoid) ([0-9]+)'::pg_catalog.text,
                                          'g'::pg_catalog.text) AS m(v)
    UNION SELECT o.oprcode::pg_catalog.oid FROM expr_trees e,
        LATERAL pg_catalog.regexp_matches(e.tree, ':opno ([0-9]+)'::pg_catalog.text, 'g'::pg_catalog.text) AS m(v)
        JOIN pg_catalog.pg_operator o ON o.oid OPERATOR(pg_catalog.=) (m.v[1])::pg_catalog.oid
    UNION SELECT o.oprcode::pg_catalog.oid FROM expr_trees e,
        LATERAL pg_catalog.regexp_matches(e.tree, ':opnos \(o([ 0-9]+)\)'::pg_catalog.text, 'g'::pg_catalog.text) AS m(v),
        LATERAL pg_catalog.regexp_split_to_table(pg_catalog.btrim(m.v[1]), ' +'::pg_catalog.text) AS x(op)
        JOIN pg_catalog.pg_operator o ON o.oid OPERATOR(pg_catalog.=) (x.op)::pg_catalog.oid
),
identities AS (SELECT rl.oid FROM rl UNION SELECT sess.oid FROM sess),
-- Pinned bridge routines and triggers: byte-identical copies of the lists in
-- sql/aiven/defaultdb_precheck.sql (tests/test_defaultdb_roles_workflow.py checks it).
bridge_functions(sig) AS (VALUES
    ('canonical_alias_revision_guard||trigger|plpgsql|false|-|70c78d2f97f2966f7611a87dfb58f946'),
    ('canonical_reject_mutation||trigger|plpgsql|false|-|1ed4bc2ff6613135008b8adc6211fd3f'),
    ('seal_evaluation_protocol||trigger|plpgsql|false|-|7377c626fd29b2fd4ef38c13d35bb323'),
    ('seal_pilot_row||trigger|plpgsql|false|-|fe277f0013ff9ac7ff637742361dbca4')
),
-- 0010 (#199): its four functions (pinned search_path; only the budget trigger is SECURITY DEFINER,
-- owned by the ledger owner) and twelve triggers. 0009 adds neither.
bridge_functions_0010(sig) AS (VALUES
    ('api_football_cert_attempt_guard||trigger|plpgsql|true|{"search_path=pg_catalog, pg_temp"}|49a19e3727b56c804e8f0c3d3b85b7fc'),
    ('api_football_cert_plan_usage_rule||trigger|plpgsql|false|{"search_path=pg_catalog, pg_temp"}|7c23be9b71fd513ddaf6fef40437832d'),
    ('api_football_cert_reject_mutation||trigger|plpgsql|false|{"search_path=pg_catalog, pg_temp"}|e61761d43ca2caf61cd43e1cb5a33dd4'),
    ('api_football_cert_stamp||trigger|plpgsql|false|{"search_path=pg_catalog, pg_temp"}|c5ff7ff31ef4c10ed1b59e4f98678390')
),
bridge_triggers_0010(sig) AS (VALUES
    ('api_football_cert_campaign_attempts|api_football_cert_attempt_budget|api_football_cert_attempt_guard|7'),
    ('api_football_cert_campaign_attempts|api_football_cert_attempts_immutable|api_football_cert_reject_mutation|58'),
    ('api_football_cert_campaign_attempts|api_football_cert_attempts_stamp|api_football_cert_stamp|7'),
    ('api_football_cert_campaign_outcomes|api_football_cert_outcomes_immutable|api_football_cert_reject_mutation|58'),
    ('api_football_cert_campaign_outcomes|api_football_cert_outcomes_stamp|api_football_cert_stamp|7'),
    ('api_football_cert_campaigns|api_football_cert_campaigns_immutable|api_football_cert_reject_mutation|58'),
    ('api_football_cert_campaigns|api_football_cert_campaigns_stamp|api_football_cert_stamp|7'),
    ('api_football_cert_plan_usage|api_football_cert_plan_usage_kept|api_football_cert_reject_mutation|42'),
    ('api_football_cert_plan_usage|api_football_cert_plan_usage_monotonic|api_football_cert_plan_usage_rule|23'),
    ('api_football_cert_plans|api_football_cert_plans_immutable|api_football_cert_reject_mutation|58'),
    ('api_football_cert_plans|api_football_cert_plans_stamp|api_football_cert_stamp|7'),
    ('api_football_cert_plans|api_football_cert_plans_usage_init|api_football_cert_plan_usage_rule|5')
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
state_functions(sig) AS (
    SELECT f.sig FROM bridge_functions f
    UNION ALL SELECT f.sig FROM bridge_functions_0010 f, bridge_state b WHERE b.v OPERATOR(pg_catalog.>=) 10
),
state_triggers(sig) AS (
    SELECT t.sig FROM bridge_triggers t
    UNION ALL SELECT t.sig FROM bridge_triggers_0010 t, bridge_state b WHERE b.v OPERATOR(pg_catalog.>=) 10
),
checks(code, bad) AS (
    SELECT 'roles_missing'::pg_catalog.text, ((SELECT pg_catalog.count(*) FROM rl) OPERATOR(pg_catalog.<>) 3::pg_catalog.int8)
    UNION ALL SELECT 'role_attributes', (EXISTS (SELECT 1 FROM rl WHERE rl.rolsuper OR rl.rolcreaterole OR rl.rolcreatedb
        OR rl.rolreplication OR rl.rolbypassrls))
    UNION ALL SELECT 'role_login', (EXISTS (SELECT 1 FROM rl
        WHERE (rl.rolname OPERATOR(pg_catalog.=) 'fcb_app'::pg_catalog.name AND NOT rl.rolcanlogin)
           OR (rl.rolname OPERATOR(pg_catalog.=) 'fcb_signer'::pg_catalog.name AND rl.rolcanlogin)))
    UNION ALL SELECT 'role_member_of_other_role', (EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m JOIN rl ON rl.oid OPERATOR(pg_catalog.=) m.member))
    UNION ALL SELECT 'unexpected_migrator_member', (EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
        WHERE m.roleid OPERATOR(pg_catalog.=) (SELECT mig.oid FROM mig) AND m.member OPERATOR(pg_catalog.<>) (SELECT sess.oid FROM sess)))
    UNION ALL SELECT 'session_reaches_migrator', ((SELECT pg_catalog.count(*) FROM mig) OPERATOR(pg_catalog.=) 1::pg_catalog.int8
        AND (pg_catalog.pg_has_role(SESSION_USER, (SELECT mig.oid FROM mig), 'USAGE')
             OR pg_catalog.pg_has_role(SESSION_USER, (SELECT mig.oid FROM mig), 'SET')))
    UNION ALL SELECT 'role_reachability', (EXISTS (SELECT 1 FROM reach x
        WHERE x.u OR x.s OR (x.m AND x.member OPERATOR(pg_catalog.<>) (SELECT sess.oid FROM sess))))
    UNION ALL SELECT 'unexpected_app_or_signer_member', (EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
        WHERE m.roleid OPERATOR(pg_catalog.=) ANY (ARRAY[(SELECT app.oid FROM app), (SELECT sgn.oid FROM sgn)])
        AND (m.member OPERATOR(pg_catalog.<>) (SELECT sess.oid FROM sess) OR m.inherit_option OR m.set_option)))
    UNION ALL SELECT 'membership_inventory', (EXISTS (
        SELECT i.roleid, i.member, i.admin_option, i.inherit_option, i.set_option, i.grantor FROM inventory i
        EXCEPT SELECT e.roleid, e.member, e.admin_option, e.inherit_option, e.set_option, e.grantor FROM expected_inventory e)
        OR EXISTS (
        SELECT e.roleid, e.member, e.admin_option, e.inherit_option, e.set_option, e.grantor FROM expected_inventory e
        EXCEPT SELECT i.roleid, i.member, i.admin_option, i.inherit_option, i.set_option, i.grantor FROM inventory i))
    UNION ALL SELECT 'escalation_path', (EXISTS (
        SELECT 1 FROM rl, identities t
        WHERE rl.oid OPERATOR(pg_catalog.<>) t.oid AND pg_catalog.pg_has_role(rl.oid, t.oid, 'MEMBER')))
    UNION ALL SELECT 'owner_model', (EXISTS (SELECT 1 FROM rels c WHERE c.relowner IS DISTINCT FROM (SELECT owner_oid.oid FROM owner_oid))
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace
        WHERE p.proowner IS DISTINCT FROM (SELECT owner_oid.oid FROM owner_oid))
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_type t JOIN ns ON ns.oid OPERATOR(pg_catalog.=) t.typnamespace
        WHERE t.typowner IS DISTINCT FROM (SELECT owner_oid.oid FROM owner_oid))
        OR (NOT (SELECT phase.two FROM phase) AND EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d
            WHERE d.refclassid OPERATOR(pg_catalog.=) 'pg_catalog.pg_authid'::pg_catalog.regclass
              AND d.refobjid OPERATOR(pg_catalog.=) (SELECT mig.oid FROM mig)
              AND d.deptype OPERATOR(pg_catalog.=) ANY (ARRAY['o', 'a', 'i']::pg_catalog."char"[]))))
    UNION ALL SELECT 'unexpected_relation_grant', (EXISTS (
        SELECT g.relname, g.grantee, g.priv FROM rel_grants g
        EXCEPT SELECT e.relname, e.grantee, e.priv FROM expected_grants e))
    UNION ALL SELECT 'missing_runtime_grant', (EXISTS (
        SELECT e.relname, e.grantee, e.priv FROM expected_grants e
        EXCEPT SELECT g.relname, g.grantee, g.priv FROM rel_grants g))
    UNION ALL SELECT 'column_grant', (EXISTS (
        SELECT g.relname, g.attname, g.grantee, g.priv FROM column_grants g
        EXCEPT (SELECT e.relname, e.attname, (SELECT app.oid FROM app), e.priv FROM expected_column_grants e, bridge_state b
        WHERE e.since OPERATOR(pg_catalog.<=) b.v
        UNION ALL SELECT g.relname, g.attname, w.oid, g.priv FROM cert_runner_column_grants g, cert_runner w))
        OR EXISTS (
        SELECT e.relname, e.attname, (SELECT app.oid FROM app), e.priv FROM expected_column_grants e, bridge_state b
        WHERE e.since OPERATOR(pg_catalog.<=) b.v
        UNION ALL SELECT g.relname, g.attname, w.oid, g.priv FROM cert_runner_column_grants g, cert_runner w
        EXCEPT SELECT g.relname, g.attname, g.grantee, g.priv FROM column_grants g))
    UNION ALL SELECT 'bridge_state', ((SELECT b.v FROM bridge_state b) OPERATOR(pg_catalog.=) 0)
    UNION ALL SELECT 'routine_grant', (EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace,
        LATERAL pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f'::pg_catalog."char", p.proowner))) a
        WHERE a.grantee OPERATOR(pg_catalog.<>) p.proowner))
    UNION ALL SELECT 'schema_acl', (EXISTS (
        SELECT g.grantee, g.priv FROM schema_grants g EXCEPT SELECT e.grantee, e.priv FROM expected_schema_grants e)
        OR EXISTS (SELECT e.grantee, e.priv FROM expected_schema_grants e EXCEPT SELECT g.grantee, g.priv FROM schema_grants g))
    UNION ALL SELECT 'signer_schema_usage', ((SELECT pg_catalog.count(*) FROM sgn) OPERATOR(pg_catalog.=) 1::pg_catalog.int8
        AND pg_catalog.has_schema_privilege((SELECT sgn.oid FROM sgn), 'public', 'USAGE'))
    UNION ALL SELECT 'app_schema_create', ((SELECT pg_catalog.count(*) FROM app) OPERATOR(pg_catalog.=) 1::pg_catalog.int8
        AND pg_catalog.has_schema_privilege((SELECT app.oid FROM app), 'public', 'CREATE'))
    UNION ALL SELECT 'database_create', (EXISTS (SELECT 1 FROM rl
        WHERE pg_catalog.has_database_privilege(rl.oid, pg_catalog.current_database(), 'CREATE')))
    UNION ALL SELECT 'default_acl_grant', (EXISTS (
        SELECT 1 FROM pg_catalog.pg_default_acl d, LATERAL pg_catalog.aclexplode(d.defaclacl) a
        WHERE a.grantee OPERATOR(pg_catalog.<>) d.defaclrole))
    UNION ALL SELECT 'default_function_revoke_missing', ((SELECT phase.two FROM phase)
        AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_default_acl d
        WHERE d.defaclrole OPERATOR(pg_catalog.=) (SELECT mig.oid FROM mig)
        AND d.defaclnamespace OPERATOR(pg_catalog.=) 0::pg_catalog.oid
        AND d.defaclobjtype OPERATOR(pg_catalog.=) 'f'::pg_catalog."char"
        AND NOT EXISTS (SELECT 1 FROM pg_catalog.aclexplode(d.defaclacl) a
        WHERE a.grantee OPERATOR(pg_catalog.=) 0::pg_catalog.oid)))
    UNION ALL SELECT 'expression_functions', (EXISTS (SELECT 1 FROM expr_function_oids f
        WHERE f.oid OPERATOR(pg_catalog.<>) 0::pg_catalog.oid AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN allowed_expr_functions_state a
            ON a.sig OPERATOR(pg_catalog.=) (p.proname::pg_catalog.text OPERATOR(pg_catalog.||) '('::pg_catalog.text
               OPERATOR(pg_catalog.||) pg_catalog.pg_get_function_identity_arguments(p.oid) OPERATOR(pg_catalog.||) ')'::pg_catalog.text)
            WHERE p.oid OPERATOR(pg_catalog.=) f.oid
              AND p.pronamespace OPERATOR(pg_catalog.=) 'pg_catalog'::pg_catalog.regnamespace)))
    UNION ALL SELECT 'role_settings', (EXISTS (SELECT 1 FROM pg_catalog.pg_db_role_setting rs
        WHERE rs.setrole OPERATOR(pg_catalog.=) ANY (ARRAY(SELECT rl.oid FROM rl))))
    UNION ALL SELECT 'routine_set', ((SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace)
            OPERATOR(pg_catalog.<>) (SELECT pg_catalog.count(*) FROM state_functions)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace
            JOIN pg_catalog.pg_language l ON l.oid OPERATOR(pg_catalog.=) p.prolang
            WHERE p.prokind OPERATOR(pg_catalog.<>) 'f'::pg_catalog."char"
               -- SECURITY DEFINER and SET are part of the pinned signature (only the 0010 budget trigger has them)
               OR NOT EXISTS (SELECT 1 FROM state_functions b WHERE b.sig OPERATOR(pg_catalog.=) (
                    p.proname::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) pg_catalog.pg_get_function_identity_arguments(p.oid) OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) pg_catalog.format_type(p.prorettype, NULL::pg_catalog.int4) OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) l.lanname::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) p.prosecdef::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) COALESCE(p.proconfig::pg_catalog.text, '-'::pg_catalog.text) OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) pg_catalog.md5(p.prosrc)))))
    UNION ALL SELECT 'trigger_set', ((SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger tg JOIN rels c ON c.oid OPERATOR(pg_catalog.=) tg.tgrelid
            WHERE NOT tg.tgisinternal) OPERATOR(pg_catalog.<>) (SELECT pg_catalog.count(*) FROM state_triggers)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_trigger tg JOIN rels c ON c.oid OPERATOR(pg_catalog.=) tg.tgrelid
            JOIN pg_catalog.pg_proc f ON f.oid OPERATOR(pg_catalog.=) tg.tgfoid
            WHERE NOT tg.tgisinternal AND (
                f.pronamespace OPERATOR(pg_catalog.<>) (SELECT ns.oid FROM ns)
                OR tg.tgenabled OPERATOR(pg_catalog.<>) 'O'::pg_catalog."char"
                OR tg.tgnargs OPERATOR(pg_catalog.<>) 0::pg_catalog.int2
                OR tg.tgattr::pg_catalog.text OPERATOR(pg_catalog.<>) ''::pg_catalog.text
                OR tg.tgqual IS NOT NULL
                OR tg.tgconstraint OPERATOR(pg_catalog.<>) 0::pg_catalog.oid
                OR tg.tgoldtable IS NOT NULL OR tg.tgnewtable IS NOT NULL
                OR NOT EXISTS (SELECT 1 FROM state_triggers b WHERE b.sig OPERATOR(pg_catalog.=) (
                    c.relname::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) tg.tgname::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) f.proname::pg_catalog.text OPERATOR(pg_catalog.||) '|'::pg_catalog.text
                    OPERATOR(pg_catalog.||) tg.tgtype::pg_catalog.text)))))
    UNION ALL SELECT 'foreign_objects', (EXISTS (SELECT 1 FROM pg_catalog.pg_type t JOIN ns ON ns.oid OPERATOR(pg_catalog.=) t.typnamespace
            WHERE NOT EXISTS (SELECT 1 FROM rels c JOIN pg_catalog.pg_type rt ON rt.oid OPERATOR(pg_catalog.=) (
                    SELECT x.reltype FROM pg_catalog.pg_class x WHERE x.oid OPERATOR(pg_catalog.=) c.oid)
                WHERE rt.oid OPERATOR(pg_catalog.=) t.oid OR rt.typarray OPERATOR(pg_catalog.=) t.oid))
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_operator o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.oprnamespace)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_opclass o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.opcnamespace)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_opfamily o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.opfnamespace)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_collation o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.collnamespace)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_conversion o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.connamespace)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_ts_config o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.cfgnamespace)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_ts_dict o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.dictnamespace)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_ts_parser o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.prsnamespace)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_ts_template o JOIN ns ON ns.oid OPERATOR(pg_catalog.=) o.tmplnamespace)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_extension x JOIN ns ON ns.oid OPERATOR(pg_catalog.=) x.extnamespace)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_cast k
            WHERE EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace
                          WHERE p.oid OPERATOR(pg_catalog.=) k.castfunc)
               OR EXISTS (SELECT 1 FROM pg_catalog.pg_type t JOIN ns ON ns.oid OPERATOR(pg_catalog.=) t.typnamespace
                          WHERE t.oid OPERATOR(pg_catalog.=) k.castsource OR t.oid OPERATOR(pg_catalog.=) k.casttarget)))
    UNION ALL SELECT 'relation_hooks', (EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite rw JOIN rels c ON c.oid OPERATOR(pg_catalog.=) rw.ev_class)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_policy pol JOIN rels c ON c.oid OPERATOR(pg_catalog.=) pol.polrelid)
        OR EXISTS (SELECT 1 FROM rels c WHERE c.relkind OPERATOR(pg_catalog.<>) ALL (ARRAY['r', 'S', 'i', 't']::pg_catalog."char"[]))
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN rels r ON r.oid OPERATOR(pg_catalog.=) c.oid
                   WHERE c.relrowsecurity OR c.relforcerowsecurity)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_inherits ih JOIN rels c
                   ON c.oid OPERATOR(pg_catalog.=) ih.inhrelid OR c.oid OPERATOR(pg_catalog.=) ih.inhparent)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_event_trigger et WHERE et.evtenabled OPERATOR(pg_catalog.<>) 'D'::pg_catalog."char"))
)
SELECT CASE
    WHEN (SELECT pg_catalog.count(*) FROM rl) OPERATOR(pg_catalog.=) 0::pg_catalog.int8 THEN 'ROLES_ABSENT'::pg_catalog.text
    WHEN NOT EXISTS (SELECT 1 FROM checks WHERE checks.bad) AND (SELECT phase.two FROM phase) THEN 'HARDENED'::pg_catalog.text
    WHEN NOT EXISTS (SELECT 1 FROM checks WHERE checks.bad) THEN 'HARDENED_PHASE1'::pg_catalog.text
    ELSE 'NOT_HARDENED:'::pg_catalog.text OPERATOR(pg_catalog.||)
         (SELECT pg_catalog.string_agg(checks.code, ','::pg_catalog.text ORDER BY checks.code) FROM checks WHERE checks.bad)
END AS roles_state,
       (SELECT CASE b.v WHEN 0 THEN 'BRIDGE_STATE_INVALID'::pg_catalog.text
               ELSE 'BRIDGE_0001_' OPERATOR(pg_catalog.||) pg_catalog.lpad(b.v::pg_catalog.text, 4, '0'::pg_catalog.text) END
        FROM bridge_state b) AS bridge_state \gset fcb_
\echo :fcb_roles_state
