-- AF-11 (owner decision 2026-11-11): bridge 0010 is applied only on top of bridge 0009 (PR #198,
-- 0009_oauth_throttle_buckets.sql). First statement of the 0010 transaction, right after the target
-- guard and BEFORE the bridge 0001-0010 script, which would otherwise also apply a missing 0009:
-- the recorded 0009 row (name and checksum of the repository file, pinned here) must exist and
-- nothing above 0010 may be recorded. The workflow precheck checks the same thing read-only.
-- If #198 were never merged, 0010 is NOT applied: renumbering only after re-checking the real
-- production baseline (AF-11), never here. Catalog-qualified.
DO $require0009$
BEGIN
    IF pg_catalog.to_regclass('public.schema_migrations') IS NULL
       OR NOT EXISTS (SELECT 1 FROM public.schema_migrations m
                      WHERE m.version OPERATOR(pg_catalog.=) 9
                        AND m.name OPERATOR(pg_catalog.=) '0009_oauth_throttle_buckets.sql'::pg_catalog.text
                        AND m.sha256 OPERATOR(pg_catalog.=) '2be8a8b2961b0516c9aba11cbddbcf8001e337de3d3bb46abf7cb932c7f5a56f'::pg_catalog.text)
       OR EXISTS (SELECT 1 FROM public.schema_migrations m WHERE m.version OPERATOR(pg_catalog.>) 10) THEN
        RAISE EXCEPTION 'FCB_CERT_LEDGER_REQUIRES_BRIDGE_0009';
    END IF;
END $require0009$;
