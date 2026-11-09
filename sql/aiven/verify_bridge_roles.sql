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
--   ownership  every relation, routine and type in public is owned by fcb_migrator;
--   tables     the only non-owner privileges in public are the runtime allowlist of fcb_app
--              (finalize_bridge_privileges.sql); no column privilege; no PUBLIC or signer grant;
--   routines   no non-owner EXECUTE (PUBLIC revoked);
--   schema     public: fcb_migrator USAGE+CREATE, fcb_app USAGE, nothing for PUBLIC/fcb_signer
--              (grants held by the schema owner or the session identity are not judged);
--   database   neither fcb_app nor fcb_signer may CREATE (schemas) in the database;
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
runtime_tables(relname, privs) AS (VALUES
    ('schema_migrations'::pg_catalog.text, ARRAY['SELECT']::pg_catalog.text[]),
    ('oauth_authorization_codes', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']),
    ('oauth_access_tokens', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']),
    ('oauth_refresh_tokens', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']),
    ('oauth_claude_dcr_clients', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']),
    ('browser_session_leases', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']),
    ('api_football_quota_counters', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']),
    ('api_football_quota_leases', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']),
    ('api_football_quota_attempts', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']),
    ('sxbet_rate_attempts', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']),
    ('sxbet_rate_cooldowns', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'])
),
expected_grants AS (
    SELECT t.relname, (SELECT app.oid FROM app) AS grantee, p.priv
    FROM runtime_tables t, LATERAL pg_catalog.unnest(t.privs) AS p(priv)
),
schema_grants AS (
    SELECT a.grantee, a.privilege_type::pg_catalog.text AS priv
    FROM ns, LATERAL pg_catalog.aclexplode(COALESCE(ns.nspacl, pg_catalog.acldefault('n'::pg_catalog."char", ns.nspowner))) a
    WHERE a.grantee OPERATOR(pg_catalog.<>) ns.nspowner AND a.grantee OPERATOR(pg_catalog.<>) (SELECT sess.oid FROM sess)
),
expected_schema_grants(grantee, priv) AS (
    SELECT (SELECT mig.oid FROM mig), 'USAGE'::pg_catalog.text
    UNION ALL SELECT (SELECT mig.oid FROM mig), 'CREATE'::pg_catalog.text
    UNION ALL SELECT (SELECT app.oid FROM app), 'USAGE'::pg_catalog.text
),
checks(code, bad) AS (
    SELECT 'roles_missing'::pg_catalog.text, (SELECT pg_catalog.count(*) FROM rl) OPERATOR(pg_catalog.<>) 3::pg_catalog.int8
    UNION ALL SELECT 'role_attributes', EXISTS (SELECT 1 FROM rl WHERE rl.rolsuper OR rl.rolcreaterole OR rl.rolcreatedb
                                                                   OR rl.rolreplication OR rl.rolbypassrls)
    UNION ALL SELECT 'role_login', EXISTS (SELECT 1 FROM rl WHERE rl.rolcanlogin OPERATOR(pg_catalog.<>)
                                           (rl.rolname OPERATOR(pg_catalog.<>) 'fcb_signer'::pg_catalog.name))
    UNION ALL SELECT 'role_member_of_other_role', EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m JOIN rl ON rl.oid OPERATOR(pg_catalog.=) m.member)
    UNION ALL SELECT 'unexpected_migrator_member', EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
        WHERE m.roleid OPERATOR(pg_catalog.=) (SELECT mig.oid FROM mig) AND m.member OPERATOR(pg_catalog.<>) (SELECT sess.oid FROM sess))
    UNION ALL SELECT 'session_lacks_migrator_privileges', (SELECT pg_catalog.count(*) FROM mig) OPERATOR(pg_catalog.=) 1::pg_catalog.int8
        AND NOT pg_catalog.pg_has_role(SESSION_USER, (SELECT mig.oid FROM mig), 'USAGE')
    UNION ALL SELECT 'unexpected_app_or_signer_member', EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
        WHERE m.roleid OPERATOR(pg_catalog.=) ANY (ARRAY[(SELECT app.oid FROM app), (SELECT sgn.oid FROM sgn)])
          AND (m.member OPERATOR(pg_catalog.<>) (SELECT sess.oid FROM sess) OR m.inherit_option OR m.set_option))
    UNION ALL SELECT 'owner_not_migrator',
        EXISTS (SELECT 1 FROM rels c WHERE c.relowner IS DISTINCT FROM (SELECT mig.oid FROM mig))
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace
                   WHERE p.proowner IS DISTINCT FROM (SELECT mig.oid FROM mig))
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_type t JOIN ns ON ns.oid OPERATOR(pg_catalog.=) t.typnamespace
                   WHERE t.typowner IS DISTINCT FROM (SELECT mig.oid FROM mig))
    UNION ALL SELECT 'unexpected_relation_grant', EXISTS (
        SELECT g.relname, g.grantee, g.priv FROM rel_grants g
        EXCEPT SELECT e.relname, e.grantee, e.priv FROM expected_grants e)
    UNION ALL SELECT 'missing_runtime_grant', EXISTS (
        SELECT e.relname, e.grantee, e.priv FROM expected_grants e
        EXCEPT SELECT g.relname, g.grantee, g.priv FROM rel_grants g)
    UNION ALL SELECT 'column_grant', EXISTS (SELECT 1 FROM rels c JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) c.oid
                                             WHERE a.attacl IS NOT NULL)
    UNION ALL SELECT 'routine_grant', EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace,
        LATERAL pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f'::pg_catalog."char", p.proowner))) a
        WHERE a.grantee OPERATOR(pg_catalog.<>) p.proowner)
    UNION ALL SELECT 'schema_acl', EXISTS (
        SELECT g.grantee, g.priv FROM schema_grants g EXCEPT SELECT e.grantee, e.priv FROM expected_schema_grants e)
        OR EXISTS (SELECT e.grantee, e.priv FROM expected_schema_grants e EXCEPT SELECT g.grantee, g.priv FROM schema_grants g)
    UNION ALL SELECT 'signer_schema_usage', (SELECT pg_catalog.count(*) FROM sgn) OPERATOR(pg_catalog.=) 1::pg_catalog.int8
        AND pg_catalog.has_schema_privilege((SELECT sgn.oid FROM sgn), 'public', 'USAGE')
    UNION ALL SELECT 'app_schema_create', (SELECT pg_catalog.count(*) FROM app) OPERATOR(pg_catalog.=) 1::pg_catalog.int8
        AND pg_catalog.has_schema_privilege((SELECT app.oid FROM app), 'public', 'CREATE')
    UNION ALL SELECT 'database_create', EXISTS (SELECT 1 FROM rl
        WHERE rl.rolname OPERATOR(pg_catalog.<>) 'fcb_migrator'::pg_catalog.name
          AND pg_catalog.has_database_privilege(rl.oid, pg_catalog.current_database(), 'CREATE'))
    UNION ALL SELECT 'default_acl_grant', EXISTS (
        SELECT 1 FROM pg_catalog.pg_default_acl d, LATERAL pg_catalog.aclexplode(d.defaclacl) a
        WHERE a.grantee OPERATOR(pg_catalog.<>) d.defaclrole)
    UNION ALL SELECT 'default_function_revoke_missing', (SELECT pg_catalog.count(*) FROM mig) OPERATOR(pg_catalog.=) 1::pg_catalog.int8
        AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_default_acl d
                        WHERE d.defaclrole OPERATOR(pg_catalog.=) (SELECT mig.oid FROM mig)
                          AND d.defaclnamespace OPERATOR(pg_catalog.=) 0::pg_catalog.oid
                          AND d.defaclobjtype OPERATOR(pg_catalog.=) 'f'::pg_catalog."char"
                          AND NOT EXISTS (SELECT 1 FROM pg_catalog.aclexplode(d.defaclacl) a
                                          WHERE a.grantee OPERATOR(pg_catalog.=) 0::pg_catalog.oid))
)
SELECT CASE
    WHEN (SELECT pg_catalog.count(*) FROM rl) OPERATOR(pg_catalog.=) 0::pg_catalog.int8 THEN 'ROLES_ABSENT'::pg_catalog.text
    WHEN NOT EXISTS (SELECT 1 FROM checks WHERE checks.bad) THEN 'HARDENED'::pg_catalog.text
    ELSE 'NOT_HARDENED:'::pg_catalog.text OPERATOR(pg_catalog.||)
         (SELECT pg_catalog.string_agg(checks.code, ','::pg_catalog.text ORDER BY checks.code) FROM checks WHERE checks.bad)
END AS roles_state \gset fcb_
\echo :fcb_roles_state
