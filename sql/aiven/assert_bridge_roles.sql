-- FCB-WP-009: last file of both roles transactions. Evaluates sql/aiven/verify_bridge_roles.sql
-- and aborts the whole transaction (nothing committed) unless the result is exactly
-- :'expected_roles_state' (HARDENED_PHASE1 after phase 1, HARDENED after phase 2).
-- Same session re-assertion as the other in-transaction files (audit F3).
DO $fcbsession$
BEGIN
    IF pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) 'public'::pg_catalog.text
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER THEN
        RAISE EXCEPTION 'FCB_ROLES_SESSION_GUARD_FAILED';
    END IF;
END $fcbsession$;
\ir verify_bridge_roles.sql
SELECT pg_catalog.set_config('fcb.roles_state', :'fcb_roles_state', true) IS NOT NULL AS state_set;
SELECT pg_catalog.set_config('fcb.expected_roles_state', :'expected_roles_state', true) IS NOT NULL AS expected_set;
DO $fcbroles$
BEGIN
    IF pg_catalog.current_setting('fcb.expected_roles_state') OPERATOR(pg_catalog.<>) ALL (ARRAY['HARDENED_PHASE1', 'HARDENED']::pg_catalog.text[])
       OR pg_catalog.current_setting('fcb.roles_state') OPERATOR(pg_catalog.<>) pg_catalog.current_setting('fcb.expected_roles_state') THEN
        RAISE EXCEPTION 'FCB_ROLES_MODEL_MISMATCH %', pg_catalog.current_setting('fcb.roles_state');
    END IF;
END $fcbroles$;
