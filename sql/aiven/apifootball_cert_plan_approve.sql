-- AF-15 (owner decision 2026-11-11): register ONE approved API-FOOTBALL certification plan.
-- Run only by .github/workflows/fcb-aiven-defaultdb-apifootball-cert-plan-approve.yml, as
-- fcb_migrator, in ONE transaction after the target guard. psql variables (set by the workflow from
-- the committed plan file and the GitHub context, never typed free-form):
--   plan_sha256  SHA-256 of the plan file committed on main (equal to the dispatch input)
--   plan_kind    BATCH1 | LIVE_FULL_MATCH_SNAPSHOT
--   budget       the plan's attempts (batch1: max_attempts_total = 78; snapshot: 8)
--   approved_by  github.actor (the owner, checked by require_owner_main_dispatch.sh)
--   run_id       github.run_id
-- Append-only: INSERT ... ON CONFLICT DO NOTHING. A plan already approved is NOT renewed (no new
-- budget); a different budget or kind for the same sha is refused. approved_role and approved_at
-- are stamped by the server (0010 trigger). Catalog-qualified.
SELECT pg_catalog.set_config('fcb_cert.plan_sha256', :'plan_sha256', true),
       pg_catalog.set_config('fcb_cert.plan_kind', :'plan_kind', true),
       pg_catalog.set_config('fcb_cert.budget', :'budget', true),
       pg_catalog.set_config('fcb_cert.approved_by', :'approved_by', true),
       pg_catalog.set_config('fcb_cert.run_id', :'run_id', true);

DO $who$
BEGIN
    IF CURRENT_USER OPERATOR(pg_catalog.<>) 'fcb_migrator'::pg_catalog.name
       OR SESSION_USER OPERATOR(pg_catalog.<>) 'fcb_migrator'::pg_catalog.name THEN
        RAISE EXCEPTION 'FCB_CERT_PLAN_NOT_MIGRATOR';
    END IF;
    IF pg_catalog.to_regclass('public.api_football_cert_plans') IS NULL THEN
        RAISE EXCEPTION 'FCB_CERT_PLAN_REQUIRES_BRIDGE_0010';
    END IF;
    IF pg_catalog.current_setting('fcb_cert.plan_sha256') OPERATOR(pg_catalog.!~) '^[0-9a-f]{64}$'::pg_catalog.text
       OR pg_catalog.current_setting('fcb_cert.plan_kind') OPERATOR(pg_catalog.<>) ALL (ARRAY['BATCH1', 'LIVE_FULL_MATCH_SNAPSHOT']::pg_catalog.text[])
       OR pg_catalog.current_setting('fcb_cert.budget') OPERATOR(pg_catalog.!~) '^[1-9][0-9]?$'::pg_catalog.text
       OR pg_catalog.current_setting('fcb_cert.run_id') OPERATOR(pg_catalog.!~) '^[1-9][0-9]{0,18}$'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_CERT_PLAN_INPUT_INVALID';
    END IF;
END $who$;

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
    IF pg_catalog.has_table_privilege('fcb_app', 'public.api_football_cert_plans', 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege('fcb_app', 'public.api_football_cert_plans', 'INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'FCB_CERT_PLAN_GRANTS_UNEXPECTED';
    END IF;
END $assert$;
