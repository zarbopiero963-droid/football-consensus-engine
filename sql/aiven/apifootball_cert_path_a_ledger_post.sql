-- PATH A step 3 of 3 (last part), database `fcb_cert`, same transaction, still AS fcb_cert_owner.
-- The runner's privileges are those of the 0010 grant file, for fcb_cert_runner instead of fcb_app:
--   api_football_cert_plans              S                                 (rows only from the owner-gated approval)
--   api_football_cert_plan_usage         none                              (SECURITY DEFINER budget trigger only)
--   api_football_cert_campaigns          S, I(campaign_id, plan_sha256)
--   api_football_cert_campaign_attempts  S, I(campaign_id, call_id)        (seq from the plan counter)
--   api_football_cert_campaign_outcomes  S, I(campaign_id, seq, outcome, http_status)
-- Then the administrator's temporary SET membership is revoked and the end state is asserted.
REVOKE ALL ON TABLE public.api_football_cert_plans, public.api_football_cert_plan_usage, public.api_football_cert_campaigns,
    public.api_football_cert_campaign_attempts, public.api_football_cert_campaign_outcomes,
    public.apifootball_cert_schema_migrations FROM PUBLIC, fcb_cert_runner;
REVOKE ALL ON FUNCTION public.api_football_cert_reject_mutation(), public.api_football_cert_attempt_guard(),
    public.api_football_cert_stamp(), public.api_football_cert_plan_usage_rule() FROM PUBLIC, fcb_cert_runner;
GRANT SELECT ON TABLE public.api_football_cert_plans, public.api_football_cert_campaigns,
    public.api_football_cert_campaign_attempts, public.api_football_cert_campaign_outcomes TO fcb_cert_runner;
GRANT INSERT (campaign_id, plan_sha256) ON TABLE public.api_football_cert_campaigns TO fcb_cert_runner;
GRANT INSERT (campaign_id, call_id) ON TABLE public.api_football_cert_campaign_attempts TO fcb_cert_runner;
GRANT INSERT (campaign_id, seq, outcome, http_status) ON TABLE public.api_football_cert_campaign_outcomes TO fcb_cert_runner;
-- the tracking rows are evidence too: append-only like the ledger
CREATE OR REPLACE TRIGGER apifootball_cert_schema_migrations_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON public.apifootball_cert_schema_migrations FOR EACH STATEMENT EXECUTE FUNCTION public.api_football_cert_reject_mutation();

-- end-state assertions, still AS fcb_cert_owner (the administrator has no USAGE on this schema)
DO $fcbcertassert$
DECLARE
    tables pg_catalog.text[] := ARRAY['public.api_football_cert_plans', 'public.api_football_cert_campaigns',
                                      'public.api_football_cert_campaign_attempts',
                                      'public.api_football_cert_campaign_outcomes']::pg_catalog.text[];
    writable pg_catalog.text[] := ARRAY['', 'campaign_id,plan_sha256', 'campaign_id,call_id',
                                        'campaign_id,seq,outcome,http_status']::pg_catalog.text[];
    owner pg_catalog.oid := (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_owner'::pg_catalog.name);
    f pg_catalog.text;
    i pg_catalog.int4;
BEGIN
    IF CURRENT_USER OPERATOR(pg_catalog.<>) 'fcb_cert_owner'::pg_catalog.name
       OR pg_catalog.pg_has_role('fcb_cert_runner', 'fcb_cert_owner', 'MEMBER')
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid OPERATOR(pg_catalog.=) m.member
                  WHERE r.rolname OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner']::pg_catalog.name[]))
       OR (SELECT r.rolcanlogin FROM pg_catalog.pg_roles r WHERE r.oid OPERATOR(pg_catalog.=) owner) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_OWNER_MEMBERSHIP_UNEXPECTED';
    END IF;
    -- database: owner fcb_cert_owner; PUBLIC can neither connect nor create temp objects
    IF (SELECT d.datdba FROM pg_catalog.pg_database d WHERE d.datname OPERATOR(pg_catalog.=) 'fcb_cert'::pg_catalog.name) OPERATOR(pg_catalog.<>) owner
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_database d, LATERAL pg_catalog.aclexplode(COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))) a
                  WHERE d.datname OPERATOR(pg_catalog.=) 'fcb_cert'::pg_catalog.name AND a.grantee OPERATOR(pg_catalog.=) 0::pg_catalog.oid)
       OR pg_catalog.has_database_privilege('fcb_cert_runner', 'fcb_cert', 'CREATE,TEMPORARY')
       OR pg_catalog.has_database_privilege(SESSION_USER, 'fcb_cert', 'CREATE,TEMPORARY')
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n, LATERAL pg_catalog.aclexplode(n.nspacl) a
                  WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND a.grantee OPERATOR(pg_catalog.=) 0::pg_catalog.oid)
       OR pg_catalog.has_schema_privilege('fcb_cert_runner', 'public', 'CREATE')
       OR pg_catalog.has_schema_privilege(SESSION_USER, 'public', 'CREATE,USAGE') THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_DATABASE_ACL_UNEXPECTED';
    END IF;
    -- every relation, function and type of fcb_cert.public belongs to fcb_cert_owner; no schema other than public
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
               WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND c.relowner OPERATOR(pg_catalog.<>) owner)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
                  WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND p.proowner OPERATOR(pg_catalog.<>) owner)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n
                  WHERE n.nspname OPERATOR(pg_catalog.<>) ALL (ARRAY['public', 'pg_catalog', 'information_schema', 'pg_toast']::pg_catalog.name[])
                    AND n.nspname OPERATOR(pg_catalog.!~) '^pg_(toast_)?temp_[0-9]+$'::pg_catalog.text
                    AND n.nspname OPERATOR(pg_catalog.<>) '_avnadmin_monitor'::pg_catalog.name) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_OWNERSHIP_UNEXPECTED';
    END IF;
    -- the guards are on: 12 ledger triggers + the tracking-table trigger, all enabled
    IF (SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid OPERATOR(pg_catalog.=) t.tgrelid
        JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
        WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND NOT t.tgisinternal AND t.tgenabled OPERATOR(pg_catalog.=) 'O'::"char") OPERATOR(pg_catalog.<>) 13
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid OPERATOR(pg_catalog.=) t.tgrelid
                  JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
                  WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND t.tgenabled OPERATOR(pg_catalog.<>) 'O'::"char") THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_TRIGGERS_UNEXPECTED';
    END IF;
    -- functions: pinned search_path, only the budget trigger is SECURITY DEFINER, no EXECUTE for the runner
    FOREACH f IN ARRAY ARRAY['public.api_football_cert_reject_mutation()', 'public.api_football_cert_stamp()',
                             'public.api_football_cert_plan_usage_rule()', 'public.api_football_cert_attempt_guard()']::pg_catalog.text[] LOOP
        IF (SELECT p.proconfig FROM pg_catalog.pg_proc p WHERE p.oid OPERATOR(pg_catalog.=) f::pg_catalog.regprocedure)
             IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']::pg_catalog.text[]
           OR (SELECT p.prosecdef FROM pg_catalog.pg_proc p WHERE p.oid OPERATOR(pg_catalog.=) f::pg_catalog.regprocedure)
             IS DISTINCT FROM (f OPERATOR(pg_catalog.=) 'public.api_football_cert_attempt_guard()')
           OR pg_catalog.has_function_privilege('fcb_cert_runner', f, 'EXECUTE')
           OR pg_catalog.has_function_privilege(SESSION_USER, f, 'EXECUTE') THEN
            RAISE EXCEPTION 'FCB_CERT_PATH_A_FUNCTIONS_UNEXPECTED';
        END IF;
    END LOOP;
    -- the counter and the tracking table: nothing for the runner or the administrator
    IF pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_cert_plan_usage', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege('fcb_cert_runner', 'public.api_football_cert_plan_usage', 'SELECT,INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_table_privilege('fcb_cert_runner', 'public.apifootball_cert_schema_migrations', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_table_privilege(SESSION_USER, 'public.api_football_cert_plan_usage', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_GRANTS_UNEXPECTED';
    END IF;
    FOR i IN 1..4 LOOP
        IF NOT pg_catalog.has_table_privilege('fcb_cert_runner', tables[i], 'SELECT')
           OR pg_catalog.has_table_privilege('fcb_cert_runner', tables[i], 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege('fcb_cert_runner', tables[i], 'UPDATE,REFERENCES')
           OR pg_catalog.has_table_privilege(SESSION_USER, tables[i], 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(SESSION_USER, tables[i], 'SELECT,INSERT,UPDATE,REFERENCES')
           OR (SELECT COALESCE(pg_catalog.string_agg(a.attname::pg_catalog.text, ','::pg_catalog.text ORDER BY a.attnum), ''::pg_catalog.text)
               FROM pg_catalog.pg_attribute a
               WHERE a.attrelid OPERATOR(pg_catalog.=) tables[i]::pg_catalog.regclass
                 AND a.attnum OPERATOR(pg_catalog.>) 0 AND NOT a.attisdropped
                 AND pg_catalog.has_column_privilege('fcb_cert_runner', a.attrelid, a.attnum, 'INSERT'))
              IS DISTINCT FROM writable[i] THEN
            RAISE EXCEPTION 'FCB_CERT_PATH_A_GRANTS_UNEXPECTED';
        END IF;
    END LOOP;
    IF (SELECT pg_catalog.string_agg(m.version::pg_catalog.text OPERATOR(pg_catalog.||) '=' OPERATOR(pg_catalog.||) m.name, ' ')
        FROM public.apifootball_cert_schema_migrations m) IS DISTINCT FROM '10=0010_apifootball_cert_campaign_ledger.sql' THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_TRACKING_UNEXPECTED';
    END IF;
END $fcbcertassert$;

-- the administrator's temporary SET membership ends here, inside the transaction
RESET ROLE;
REVOKE fcb_cert_owner FROM CURRENT_USER;
DO $fcbcertrevoked$
BEGIN
    IF CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER
       OR pg_catalog.pg_has_role(SESSION_USER, 'fcb_cert_owner', 'SET')
       OR pg_catalog.pg_has_role(SESSION_USER, 'fcb_cert_owner', 'USAGE')
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid OPERATOR(pg_catalog.=) m.roleid
                  WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_owner'::pg_catalog.name AND (m.inherit_option OR m.set_option)) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_OWNER_MEMBERSHIP_UNEXPECTED';
    END IF;
END $fcbcertrevoked$;
