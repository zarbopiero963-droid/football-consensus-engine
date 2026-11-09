-- FCB-WP-009: read-only report of the bridge roles, ownership and grants in schema public.
-- Used by the roles workflow (both modes, under default_transaction_read_only=on) and by the
-- tests. Prints `key=value` lines and finally the model state of verify_bridge_roles.sql.
-- No secret, no password hash, no row content. The administrative identity is never named:
-- it appears as `session`. Every catalog relation, function and operator is schema-qualified.
WITH
sess AS (SELECT r.* FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER),
db AS (SELECT d.* FROM pg_catalog.pg_database d WHERE d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()),
ns AS (SELECT n.* FROM pg_catalog.pg_namespace n WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name),
kind(oid, label) AS (
    SELECT r.oid,
           CASE WHEN r.oid OPERATOR(pg_catalog.=) (SELECT sess.oid FROM sess) THEN 'session'::pg_catalog.text
                WHEN r.rolname OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_migrator', 'fcb_app', 'fcb_signer', 'pg_database_owner']::pg_catalog.name[])
                    THEN r.rolname::pg_catalog.text
                ELSE 'other'::pg_catalog.text END
    FROM pg_catalog.pg_roles r
    UNION ALL SELECT 0::pg_catalog.oid, 'PUBLIC'::pg_catalog.text
),
lines(o, line) AS (
    SELECT 1, 'session_superuser='::pg_catalog.text OPERATOR(pg_catalog.||) (SELECT sess.rolsuper FROM sess)::pg_catalog.text
    UNION ALL SELECT 2, 'session_createrole=' OPERATOR(pg_catalog.||) (SELECT sess.rolcreaterole FROM sess)::pg_catalog.text
    UNION ALL SELECT 3, 'database_owner=' OPERATOR(pg_catalog.||) (SELECT k.label FROM kind k, db WHERE k.oid OPERATOR(pg_catalog.=) db.datdba)
    UNION ALL SELECT 4, 'public_schema_owner=' OPERATOR(pg_catalog.||) (SELECT k.label FROM kind k, ns WHERE k.oid OPERATOR(pg_catalog.=) ns.nspowner)
    UNION ALL SELECT 5, 'session_has_public_owner_privileges=' OPERATOR(pg_catalog.||)
        pg_catalog.pg_has_role(SESSION_USER, (SELECT ns.nspowner FROM ns), 'USAGE')::pg_catalog.text
    UNION ALL SELECT 6, 'createrole_self_grant=' OPERATOR(pg_catalog.||)
        COALESCE(NULLIF(pg_catalog.current_setting('createrole_self_grant', true), ''::pg_catalog.text), '-'::pg_catalog.text)
    UNION ALL SELECT 10, 'role ' OPERATOR(pg_catalog.||) n.rolname::pg_catalog.text OPERATOR(pg_catalog.||) ' ' OPERATOR(pg_catalog.||)
        COALESCE((SELECT 'exists=true login=' OPERATOR(pg_catalog.||) r.rolcanlogin::pg_catalog.text
                  OPERATOR(pg_catalog.||) ' superuser=' OPERATOR(pg_catalog.||) r.rolsuper::pg_catalog.text
                  OPERATOR(pg_catalog.||) ' createrole=' OPERATOR(pg_catalog.||) r.rolcreaterole::pg_catalog.text
                  OPERATOR(pg_catalog.||) ' createdb=' OPERATOR(pg_catalog.||) r.rolcreatedb::pg_catalog.text
                  OPERATOR(pg_catalog.||) ' replication=' OPERATOR(pg_catalog.||) r.rolreplication::pg_catalog.text
                  OPERATOR(pg_catalog.||) ' bypassrls=' OPERATOR(pg_catalog.||) r.rolbypassrls::pg_catalog.text
                  OPERATOR(pg_catalog.||) ' member_of=' OPERATOR(pg_catalog.||) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_auth_members m
                      WHERE m.member OPERATOR(pg_catalog.=) r.oid)::pg_catalog.text
                  OPERATOR(pg_catalog.||) ' members=' OPERATOR(pg_catalog.||) COALESCE((SELECT pg_catalog.string_agg(
                      k.label OPERATOR(pg_catalog.||) '(admin=' OPERATOR(pg_catalog.||) m.admin_option::pg_catalog.text
                      OPERATOR(pg_catalog.||) ',inherit=' OPERATOR(pg_catalog.||) m.inherit_option::pg_catalog.text
                      OPERATOR(pg_catalog.||) ',set=' OPERATOR(pg_catalog.||) m.set_option::pg_catalog.text OPERATOR(pg_catalog.||) ')',
                      ';'::pg_catalog.text ORDER BY k.label, m.admin_option, m.inherit_option, m.set_option)
                      FROM pg_catalog.pg_auth_members m JOIN kind k ON k.oid OPERATOR(pg_catalog.=) m.member
                      WHERE m.roleid OPERATOR(pg_catalog.=) r.oid), '-'::pg_catalog.text)
                  OPERATOR(pg_catalog.||) ' db_connect=' OPERATOR(pg_catalog.||)
                      pg_catalog.has_database_privilege(r.oid, pg_catalog.current_database(), 'CONNECT')::pg_catalog.text
                  OPERATOR(pg_catalog.||) ' db_create=' OPERATOR(pg_catalog.||)
                      pg_catalog.has_database_privilege(r.oid, pg_catalog.current_database(), 'CREATE')::pg_catalog.text
                  OPERATOR(pg_catalog.||) ' db_temp=' OPERATOR(pg_catalog.||)
                      pg_catalog.has_database_privilege(r.oid, pg_catalog.current_database(), 'TEMPORARY')::pg_catalog.text
                  FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) n.rolname), 'exists=false')
    FROM pg_catalog.unnest(ARRAY['fcb_migrator', 'fcb_app', 'fcb_signer']::pg_catalog.name[]) AS n(rolname)
    UNION ALL SELECT 20, 'owners relations ' OPERATOR(pg_catalog.||) COALESCE((SELECT pg_catalog.string_agg(x.label OPERATOR(pg_catalog.||) '='
        OPERATOR(pg_catalog.||) x.n::pg_catalog.text, ' '::pg_catalog.text ORDER BY x.label) FROM (
            SELECT k.label, pg_catalog.count(*) AS n FROM pg_catalog.pg_class c JOIN ns ON ns.oid OPERATOR(pg_catalog.=) c.relnamespace
            JOIN kind k ON k.oid OPERATOR(pg_catalog.=) c.relowner
            WHERE c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'v', 'm', 'S', 'f']::pg_catalog."char"[]) GROUP BY k.label) x), 'none')
    UNION ALL SELECT 21, 'owners routines ' OPERATOR(pg_catalog.||) COALESCE((SELECT pg_catalog.string_agg(x.label OPERATOR(pg_catalog.||) '='
        OPERATOR(pg_catalog.||) x.n::pg_catalog.text, ' '::pg_catalog.text ORDER BY x.label) FROM (
            SELECT k.label, pg_catalog.count(*) AS n FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace
            JOIN kind k ON k.oid OPERATOR(pg_catalog.=) p.proowner GROUP BY k.label) x), 'none')
    UNION ALL SELECT 30, 'schema_public_acl ' OPERATOR(pg_catalog.||) COALESCE((SELECT pg_catalog.string_agg(
        k.label OPERATOR(pg_catalog.||) ':' OPERATOR(pg_catalog.||) a.privilege_type::pg_catalog.text, ' '::pg_catalog.text
        ORDER BY k.label, a.privilege_type)
        FROM ns, LATERAL pg_catalog.aclexplode(COALESCE(ns.nspacl, pg_catalog.acldefault('n'::pg_catalog."char", ns.nspowner))) a
        JOIN kind k ON k.oid OPERATOR(pg_catalog.=) a.grantee), 'none')
    UNION ALL SELECT 40, 'grant ' OPERATOR(pg_catalog.||) x.label OPERATOR(pg_catalog.||) ' ' OPERATOR(pg_catalog.||) x.relname
        OPERATOR(pg_catalog.||) ' ' OPERATOR(pg_catalog.||) x.privs
    FROM (SELECT k.label, c.relname::pg_catalog.text AS relname,
                 pg_catalog.string_agg(a.privilege_type::pg_catalog.text, ','::pg_catalog.text ORDER BY a.privilege_type) AS privs
          FROM pg_catalog.pg_class c JOIN ns ON ns.oid OPERATOR(pg_catalog.=) c.relnamespace,
          LATERAL pg_catalog.aclexplode(COALESCE(c.relacl, pg_catalog.acldefault(
              CASE WHEN c.relkind OPERATOR(pg_catalog.=) 'S'::pg_catalog."char" THEN 's'::pg_catalog."char" ELSE 'r'::pg_catalog."char" END,
              c.relowner))) a
          JOIN kind k ON k.oid OPERATOR(pg_catalog.=) a.grantee
          WHERE c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'v', 'm', 'S', 'f']::pg_catalog."char"[])
            AND a.grantee OPERATOR(pg_catalog.<>) c.relowner
          GROUP BY k.label, c.relname) x
    UNION ALL SELECT 50, 'routine_non_owner_grants=' OPERATOR(pg_catalog.||) (SELECT pg_catalog.count(*)
        FROM pg_catalog.pg_proc p JOIN ns ON ns.oid OPERATOR(pg_catalog.=) p.pronamespace,
        LATERAL pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f'::pg_catalog."char", p.proowner))) a
        WHERE a.grantee OPERATOR(pg_catalog.<>) p.proowner)::pg_catalog.text
    UNION ALL SELECT 51, 'default_acl ' OPERATOR(pg_catalog.||) COALESCE((SELECT pg_catalog.string_agg(
        k.label OPERATOR(pg_catalog.||) ':' OPERATOR(pg_catalog.||) d.defaclobjtype::pg_catalog.text
        OPERATOR(pg_catalog.||) ':' OPERATOR(pg_catalog.||) CASE WHEN d.defaclnamespace OPERATOR(pg_catalog.=) 0::pg_catalog.oid
            THEN 'all_schemas'::pg_catalog.text WHEN d.defaclnamespace OPERATOR(pg_catalog.=) (SELECT ns.oid FROM ns) THEN 'public'::pg_catalog.text
            ELSE 'other_schema'::pg_catalog.text END
        OPERATOR(pg_catalog.||) ':grantees=' OPERATOR(pg_catalog.||) (SELECT pg_catalog.count(*) FROM pg_catalog.aclexplode(d.defaclacl) a
            WHERE a.grantee OPERATOR(pg_catalog.<>) d.defaclrole)::pg_catalog.text,
        ' '::pg_catalog.text ORDER BY k.label, d.defaclobjtype)
        FROM pg_catalog.pg_default_acl d JOIN kind k ON k.oid OPERATOR(pg_catalog.=) d.defaclrole), 'none')
)
SELECT l.line FROM lines l ORDER BY l.o, l.line;
\ir verify_bridge_roles.sql
