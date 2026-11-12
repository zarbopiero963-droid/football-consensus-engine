-- PATH A: register ONE approved API-FOOTBALL certification plan in the ledger database fcb_cert.
-- ONE transaction, run by the administrative identity from the owner-gated approval workflow
-- (main only, protected environment, owner dispatch). The caller sets, IN THIS TRANSACTION, from the
-- committed plan file and the GitHub context (never typed free-form):
--   fcb_cert.plan_sha256  SHA-256 of the plan file committed on main
--   fcb_cert.plan_kind    BATCH1 | LIVE_FULL_MATCH_SNAPSHOT
--   fcb_cert.budget       the plan's attempts (batch1: 78; snapshot: 8)
--   fcb_cert.approved_by  github.actor (the owner)
--   fcb_cert.run_id       github.run_id
-- The administrator grants itself SET on fcb_cert_owner for this transaction only, inserts AS the
-- owner (the runner has no INSERT on plans), and revokes the grant before COMMIT (asserted).
-- Append-only: ON CONFLICT DO NOTHING; an approved plan is never renewed; a different budget or kind
-- for the same sha is refused. approved_role (= the administrator's session user) and approved_at are
-- stamped by the 0010 trigger. Catalog-qualified.
SET LOCAL search_path = public;
DO $who$
BEGIN
    IF pg_catalog.current_database() OPERATOR(pg_catalog.<>) 'fcb_cert'::pg_catalog.name
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER
       OR SESSION_USER OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner']::pg_catalog.name[])
       OR pg_catalog.pg_has_role(SESSION_USER, 'fcb_cert_owner', 'SET')
       OR pg_catalog.pg_has_role(SESSION_USER, 'fcb_cert_owner', 'USAGE') THEN
        RAISE EXCEPTION 'FCB_CERT_PLAN_GUARD_FAILED';
    END IF;
    IF COALESCE(pg_catalog.current_setting('fcb_cert.plan_sha256', true), '') OPERATOR(pg_catalog.!~) '^[0-9a-f]{64}$'::pg_catalog.text
       OR COALESCE(pg_catalog.current_setting('fcb_cert.plan_kind', true), '') OPERATOR(pg_catalog.<>) ALL (ARRAY['BATCH1', 'LIVE_FULL_MATCH_SNAPSHOT']::pg_catalog.text[])
       OR COALESCE(pg_catalog.current_setting('fcb_cert.budget', true), '') OPERATOR(pg_catalog.!~) '^[1-9][0-9]?$'::pg_catalog.text
       OR COALESCE(pg_catalog.current_setting('fcb_cert.approved_by', true), '') OPERATOR(pg_catalog.!~) '^[A-Za-z0-9][A-Za-z0-9-]{0,38}$'::pg_catalog.text
       OR COALESCE(pg_catalog.current_setting('fcb_cert.run_id', true), '') OPERATOR(pg_catalog.!~) '^[1-9][0-9]{0,18}$'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_CERT_PLAN_INPUT_INVALID';
    END IF;
END $who$;
GRANT fcb_cert_owner TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;
SET LOCAL ROLE fcb_cert_owner;
INSERT INTO public.api_football_cert_plans (plan_sha256, plan_kind, budget, approved_by, github_run_id)
SELECT pg_catalog.current_setting('fcb_cert.plan_sha256'),
       pg_catalog.current_setting('fcb_cert.plan_kind'),
       pg_catalog.current_setting('fcb_cert.budget')::pg_catalog.int4,
       pg_catalog.current_setting('fcb_cert.approved_by'),
       pg_catalog.current_setting('fcb_cert.run_id')::pg_catalog.int8
ON CONFLICT (plan_sha256) DO NOTHING;
DO $assert$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.api_football_cert_plans p
                   WHERE p.plan_sha256 OPERATOR(pg_catalog.=) pg_catalog.current_setting('fcb_cert.plan_sha256')
                     AND p.plan_kind OPERATOR(pg_catalog.=) pg_catalog.current_setting('fcb_cert.plan_kind')
                     AND p.budget OPERATOR(pg_catalog.=) pg_catalog.current_setting('fcb_cert.budget')::pg_catalog.int4) THEN
        RAISE EXCEPTION 'FCB_CERT_PLAN_CONFLICT';
    END IF;
    IF pg_catalog.has_table_privilege('fcb_cert_runner', 'public.api_football_cert_plans', 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege('fcb_cert_runner', 'public.api_football_cert_plans', 'INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'FCB_CERT_PLAN_GRANTS_UNEXPECTED';
    END IF;
END $assert$;
RESET ROLE;
REVOKE fcb_cert_owner FROM CURRENT_USER;
DO $revoked$
BEGIN
    IF pg_catalog.pg_has_role(SESSION_USER, 'fcb_cert_owner', 'SET')
       OR pg_catalog.pg_has_role(SESSION_USER, 'fcb_cert_owner', 'USAGE') THEN
        RAISE EXCEPTION 'FCB_CERT_PLAN_OWNER_MEMBERSHIP_UNEXPECTED';
    END IF;
END $revoked$;
