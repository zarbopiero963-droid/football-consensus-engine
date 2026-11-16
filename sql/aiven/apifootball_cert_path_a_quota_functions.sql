-- PATH A2 step 1b, variant R1 (owner decision D2: no direct privilege of fcb_cert_runner on the shared
-- quota tables), database `defaultdb`, ONE transaction run by the OWNER of the three shared API-FOOTBALL
-- quota-coordinator tables (after WP-009 phase 2: fcb_migrator; the install workflow requires that
-- identity). Followed, in the same transaction, by apifootball_cert_path_a_quota_assert.sql.
-- It (re)defines the FOUR functions of the schema fcb_cert_quota (created, owned by this same table
-- owner, by apifootball_cert_path_a_roles.sql, step 1): SECURITY DEFINER, search_path pinned to
-- pg_catalog, pg_temp, every relation schema-qualified, EXECUTE revoked from PUBLIC and granted to
-- fcb_cert_runner only, plus USAGE on the schema to fcb_cert_runner only. No table, column or sequence
-- privilege is granted to anybody. Idempotent (CREATE OR REPLACE keeps owner and ACL; the assert
-- checks the exact end state). Catalog-qualified. Never run for a password rotation.
-- The bodies replicate bridge/api_football/quota.py (PostgresQuota._reserve/_release/_snapshot/
-- _mark_exhausted) statement by statement on the SAME tables, rows and per-key advisory lock
-- (_advisory_lock_key: the first 16 hex digits of the key hash, sign bit cleared), so the runner and
-- the bridge serialize on the same key. Differences, all restrictive: the limits are the bridge
-- defaults pinned in the bodies (8 per minute, 80 per UTC day, 2 concurrent leases, 180 s lease;
-- bridge/api_football/config.py) and never a caller argument; the caller passes only the SHA-256 key
-- hash (64 lowercase hex, never the API key); only the session identity fcb_cert_runner may call them
-- (SESSION_USER: CURRENT_USER is the owner inside SECURITY DEFINER); the runner's lease ids carry the
-- prefix `cert-` and release() deletes only such a lease, named exactly, of the named key.
-- Limit and lease codes are raised as fixed messages (FCB_CERT_QUOTA_*), mapped back to QuotaExceeded /
-- QuotaConcurrencyBusy by scripts/apifootball_cert_batch1.py (CertQuotaCoordinator).
-- Review #201 (59d98c2) hardening:
--   * P2: ONE key hash only. The caller sets fcb_cert.pinned_key_hash (SHA-256 of the API key the runner
--     uses, computed by the installer) in this transaction; it is pinned in the fifth routine
--     fcb_cert_quota.pinned_key_hash() (LANGUAGE sql, SECURITY INVOKER, EXECUTE for the owner only, body
--     exactly SELECT '<64 hex>'::pg_catalog.text). Every function refuses any other key
--     (FCB_CERT_QUOTA_KEY_REFUSED) BEFORE taking a lock or touching a row, so a flood with other keys
--     creates no row. An existing different pin is never replaced (FCB_CERT_PATH_A_QUOTA_KEY_CHANGED: an
--     owner act outside this step). mark_exhausted('minute') is a no-op while a provider-zero marker of the
--     key is live, and every mark_exhausted prunes the key's attempts older than 60 s, so per call at most
--     8 attempt rows and one counter row per minute/day bucket can be created (same order as the bridge).
--   * P3: every function refuses unless the transaction is READ COMMITTED (FCB_CERT_QUOTA_ISOLATION_REFUSED):
--     under REPEATABLE READ / SERIALIZABLE a snapshot taken before the advisory lock could miss rows
--     committed by the previous holder and exceed the limits.
SET LOCAL search_path = pg_catalog, pg_temp;
DO $fcbcertquotaguard$
BEGIN
    IF pg_catalog.current_database() OPERATOR(pg_catalog.<>) 'defaultdb'::pg_catalog.name
       OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER
       OR (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER)
       OR SESSION_USER OPERATOR(pg_catalog.=) ANY (ARRAY['fcb_cert_owner', 'fcb_cert_runner', 'fcb_app', 'fcb_signer']::pg_catalog.name[])
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) 'fcb_cert_runner'::pg_catalog.name)
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_roles o ON o.oid OPERATOR(pg_catalog.=) c.relowner
           WHERE c.relnamespace OPERATOR(pg_catalog.=) 'public'::pg_catalog.regnamespace
             AND c.relkind OPERATOR(pg_catalog.=) 'r'::pg_catalog."char"
             AND o.rolname OPERATOR(pg_catalog.=) SESSION_USER
             AND c.relname OPERATOR(pg_catalog.=) ANY (ARRAY['api_football_quota_counters', 'api_football_quota_leases',
                                                             'api_football_quota_attempts']::pg_catalog.name[]))
          OPERATOR(pg_catalog.<>) 3::pg_catalog.int8
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_roles o ON o.oid OPERATOR(pg_catalog.=) n.nspowner
                      WHERE n.nspname OPERATOR(pg_catalog.=) 'fcb_cert_quota'::pg_catalog.name
                        AND o.rolname OPERATOR(pg_catalog.=) SESSION_USER) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_QUOTA_GUARD_FAILED';
    END IF;
    IF NOT COALESCE(pg_catalog.current_setting('fcb_cert.pinned_key_hash', true) OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'::pg_catalog.text, false) THEN
        RAISE EXCEPTION 'FCB_CERT_PATH_A_QUOTA_KEY_MISSING';
    END IF;
    -- the pinned key: created once, never silently replaced
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_proc p
               WHERE p.pronamespace OPERATOR(pg_catalog.=) 'fcb_cert_quota'::pg_catalog.regnamespace
                 AND p.proname OPERATOR(pg_catalog.=) 'pinned_key_hash'::pg_catalog.name) THEN
        IF (SELECT p.prosrc FROM pg_catalog.pg_proc p
            WHERE p.pronamespace OPERATOR(pg_catalog.=) 'fcb_cert_quota'::pg_catalog.regnamespace
              AND p.proname OPERATOR(pg_catalog.=) 'pinned_key_hash'::pg_catalog.name)
           IS DISTINCT FROM ('SELECT '::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.quote_literal(pg_catalog.current_setting('fcb_cert.pinned_key_hash'))
                             OPERATOR(pg_catalog.||) '::pg_catalog.text'::pg_catalog.text) THEN
            RAISE EXCEPTION 'FCB_CERT_PATH_A_QUOTA_KEY_CHANGED';
        END IF;
    ELSE
        EXECUTE pg_catalog.format('CREATE FUNCTION fcb_cert_quota.pinned_key_hash() RETURNS pg_catalog.text LANGUAGE sql IMMUTABLE '
                                  'SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS %L',
                                  'SELECT '::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.quote_literal(pg_catalog.current_setting('fcb_cert.pinned_key_hash'))
                                  OPERATOR(pg_catalog.||) '::pg_catalog.text'::pg_catalog.text);
    END IF;
END $fcbcertquotaguard$;
REVOKE ALL ON FUNCTION fcb_cert_quota.pinned_key_hash() FROM PUBLIC;

CREATE OR REPLACE FUNCTION fcb_cert_quota.reserve(p_key_hash pg_catalog.text)
RETURNS pg_catalog.text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fcbcertreserve$
DECLARE
    v_epoch pg_catalog.int8;
    v_active pg_catalog.int8;
    v_day_used pg_catalog.int4;
    v_recent pg_catalog.int8;
    v_secs pg_catalog.int4;
    v_got pg_catalog.int4;
    v_lease pg_catalog.text;
BEGIN
    IF SESSION_USER OPERATOR(pg_catalog.<>) 'fcb_cert_runner'::pg_catalog.name THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_IDENTITY_REFUSED';
    END IF;
    IF pg_catalog.current_setting('transaction_isolation') OPERATOR(pg_catalog.<>) 'read committed'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_ISOLATION_REFUSED';
    END IF;
    IF NOT COALESCE(p_key_hash OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'::pg_catalog.text, false) THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_BAD_KEY_HASH';
    END IF;
    IF p_key_hash OPERATOR(pg_catalog.<>) fcb_cert_quota.pinned_key_hash() THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_KEY_REFUSED';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        ((('x'::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.substr(p_key_hash, 1, 16))::pg_catalog.bit(64))::pg_catalog.int8)
        OPERATOR(pg_catalog.&) 9223372036854775807::pg_catalog.int8);
    v_epoch := EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::pg_catalog.int8;
    DELETE FROM public.api_football_quota_leases l
     WHERE l.api_key_hash OPERATOR(pg_catalog.=) p_key_hash AND l.expires_at OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp();
    SELECT pg_catalog.count(*) INTO v_active FROM public.api_football_quota_leases l
     WHERE l.api_key_hash OPERATOR(pg_catalog.=) p_key_hash;
    IF v_active OPERATOR(pg_catalog.>=) 2::pg_catalog.int8 THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_BUSY';
    END IF;
    SELECT c.used INTO v_day_used FROM public.api_football_quota_counters c
     WHERE c.api_key_hash OPERATOR(pg_catalog.=) p_key_hash AND c.window_kind OPERATOR(pg_catalog.=) 'day'::pg_catalog.text
       AND c.bucket OPERATOR(pg_catalog.=) (v_epoch OPERATOR(pg_catalog./) 86400::pg_catalog.int8);
    IF FOUND AND v_day_used OPERATOR(pg_catalog.>=) 80 THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_DAY %', v_epoch;
    END IF;
    DELETE FROM public.api_football_quota_attempts a
     WHERE a.api_key_hash OPERATOR(pg_catalog.=) p_key_hash
       AND a.created_at OPERATOR(pg_catalog.<=) (pg_catalog.clock_timestamp() OPERATOR(pg_catalog.-) '60 seconds'::pg_catalog.interval);
    SELECT pg_catalog.count(*) INTO v_recent FROM public.api_football_quota_attempts a
     WHERE a.api_key_hash OPERATOR(pg_catalog.=) p_key_hash
       AND a.created_at OPERATOR(pg_catalog.>) (pg_catalog.clock_timestamp() OPERATOR(pg_catalog.-) '60 seconds'::pg_catalog.interval);
    IF v_recent OPERATOR(pg_catalog.>=) 8::pg_catalog.int8 THEN
        SELECT GREATEST(1::pg_catalog.numeric, pg_catalog.ceil(EXTRACT(EPOCH FROM (a.created_at OPERATOR(pg_catalog.+) '60 seconds'::pg_catalog.interval
                   OPERATOR(pg_catalog.-) pg_catalog.clock_timestamp()))))::pg_catalog.int4
          INTO v_secs FROM public.api_football_quota_attempts a
         WHERE a.api_key_hash OPERATOR(pg_catalog.=) p_key_hash
           AND a.created_at OPERATOR(pg_catalog.>) (pg_catalog.clock_timestamp() OPERATOR(pg_catalog.-) '60 seconds'::pg_catalog.interval)
         ORDER BY a.created_at ASC OFFSET (v_recent OPERATOR(pg_catalog.-) 8::pg_catalog.int8) LIMIT 1;
        RAISE EXCEPTION 'FCB_CERT_QUOTA_MINUTE rolling %', COALESCE(v_secs::pg_catalog.text, '-'::pg_catalog.text);
    END IF;
    IF EXISTS (SELECT 1 FROM public.api_football_quota_attempts a
               WHERE a.api_key_hash OPERATOR(pg_catalog.=) p_key_hash
                 AND a.attempt_id OPERATOR(pg_catalog.~~) 'provider-zero-%'::pg_catalog.text
                 AND a.created_at OPERATOR(pg_catalog.>) (pg_catalog.clock_timestamp() OPERATOR(pg_catalog.-) '60 seconds'::pg_catalog.interval)) THEN
        SELECT GREATEST(1::pg_catalog.numeric, pg_catalog.ceil(EXTRACT(EPOCH FROM (pg_catalog.max(a.created_at) OPERATOR(pg_catalog.+) '60 seconds'::pg_catalog.interval
                   OPERATOR(pg_catalog.-) pg_catalog.clock_timestamp()))))::pg_catalog.int4
          INTO v_secs FROM public.api_football_quota_attempts a
         WHERE a.api_key_hash OPERATOR(pg_catalog.=) p_key_hash
           AND a.attempt_id OPERATOR(pg_catalog.~~) 'provider-zero-%'::pg_catalog.text
           AND a.created_at OPERATOR(pg_catalog.>) (pg_catalog.clock_timestamp() OPERATOR(pg_catalog.-) '60 seconds'::pg_catalog.interval);
        RAISE EXCEPTION 'FCB_CERT_QUOTA_MINUTE provider_zero %', COALESCE(v_secs::pg_catalog.text, '-'::pg_catalog.text);
    END IF;
    INSERT INTO public.api_football_quota_counters AS c (api_key_hash, window_kind, bucket, used)
    VALUES (p_key_hash, 'minute'::pg_catalog.text, v_epoch OPERATOR(pg_catalog./) 60::pg_catalog.int8, 1)
    ON CONFLICT (api_key_hash, window_kind, bucket)
    DO UPDATE SET used = c.used OPERATOR(pg_catalog.+) 1 WHERE c.used OPERATOR(pg_catalog.<) 8
    RETURNING c.used INTO v_got;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_MINUTE bucket %', 60::pg_catalog.int8 OPERATOR(pg_catalog.-) (v_epoch OPERATOR(pg_catalog.%) 60::pg_catalog.int8);
    END IF;
    INSERT INTO public.api_football_quota_counters AS c (api_key_hash, window_kind, bucket, used)
    VALUES (p_key_hash, 'day'::pg_catalog.text, v_epoch OPERATOR(pg_catalog./) 86400::pg_catalog.int8, 1)
    ON CONFLICT (api_key_hash, window_kind, bucket)
    DO UPDATE SET used = c.used OPERATOR(pg_catalog.+) 1 WHERE c.used OPERATOR(pg_catalog.<) 80
    RETURNING c.used INTO v_got;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_DAY %', v_epoch;
    END IF;
    v_lease := 'cert-'::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.translate(pg_catalog.encode(pg_catalog.substr(
        pg_catalog.uuid_send(pg_catalog.gen_random_uuid()) OPERATOR(pg_catalog.||) pg_catalog.uuid_send(pg_catalog.gen_random_uuid()),
        1, 24), 'base64'::pg_catalog.text), '+/'::pg_catalog.text, '-_'::pg_catalog.text);
    INSERT INTO public.api_football_quota_attempts (attempt_id, api_key_hash) VALUES (v_lease, p_key_hash);
    INSERT INTO public.api_football_quota_leases (lease_id, api_key_hash, expires_at)
    VALUES (v_lease, p_key_hash, pg_catalog.clock_timestamp() OPERATOR(pg_catalog.+) '180 seconds'::pg_catalog.interval);
    RETURN v_lease;
END
$fcbcertreserve$;

CREATE OR REPLACE FUNCTION fcb_cert_quota.release(p_key_hash pg_catalog.text, p_lease_id pg_catalog.text)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fcbcertrelease$
BEGIN
    IF SESSION_USER OPERATOR(pg_catalog.<>) 'fcb_cert_runner'::pg_catalog.name THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_IDENTITY_REFUSED';
    END IF;
    IF pg_catalog.current_setting('transaction_isolation') OPERATOR(pg_catalog.<>) 'read committed'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_ISOLATION_REFUSED';
    END IF;
    IF NOT COALESCE(p_key_hash OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'::pg_catalog.text, false) THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_BAD_KEY_HASH';
    END IF;
    IF p_key_hash OPERATOR(pg_catalog.<>) fcb_cert_quota.pinned_key_hash() THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_KEY_REFUSED';
    END IF;
    IF NOT COALESCE(p_lease_id OPERATOR(pg_catalog.~) '^cert-[0-9A-Za-z_-]{32}$'::pg_catalog.text, false) THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_BAD_LEASE_ID';
    END IF;
    DELETE FROM public.api_football_quota_leases l
     WHERE l.lease_id OPERATOR(pg_catalog.=) p_lease_id AND l.api_key_hash OPERATOR(pg_catalog.=) p_key_hash;
END
$fcbcertrelease$;

CREATE OR REPLACE FUNCTION fcb_cert_quota.snapshot(p_key_hash pg_catalog.text)
RETURNS TABLE (remaining_minute pg_catalog.int4, remaining_day pg_catalog.int4, active_leases pg_catalog.int4)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fcbcertsnapshot$
DECLARE
    v_epoch pg_catalog.int8;
    v_minute pg_catalog.int4;
    v_day pg_catalog.int4;
    v_rolling pg_catalog.int4;
BEGIN
    IF SESSION_USER OPERATOR(pg_catalog.<>) 'fcb_cert_runner'::pg_catalog.name THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_IDENTITY_REFUSED';
    END IF;
    IF pg_catalog.current_setting('transaction_isolation') OPERATOR(pg_catalog.<>) 'read committed'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_ISOLATION_REFUSED';
    END IF;
    IF NOT COALESCE(p_key_hash OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'::pg_catalog.text, false) THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_BAD_KEY_HASH';
    END IF;
    IF p_key_hash OPERATOR(pg_catalog.<>) fcb_cert_quota.pinned_key_hash() THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_KEY_REFUSED';
    END IF;
    v_epoch := EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::pg_catalog.int8;
    v_minute := COALESCE((SELECT c.used FROM public.api_football_quota_counters c
                          WHERE c.api_key_hash OPERATOR(pg_catalog.=) p_key_hash AND c.window_kind OPERATOR(pg_catalog.=) 'minute'::pg_catalog.text
                            AND c.bucket OPERATOR(pg_catalog.=) (v_epoch OPERATOR(pg_catalog./) 60::pg_catalog.int8)), 0);
    v_day := COALESCE((SELECT c.used FROM public.api_football_quota_counters c
                       WHERE c.api_key_hash OPERATOR(pg_catalog.=) p_key_hash AND c.window_kind OPERATOR(pg_catalog.=) 'day'::pg_catalog.text
                         AND c.bucket OPERATOR(pg_catalog.=) (v_epoch OPERATOR(pg_catalog./) 86400::pg_catalog.int8)), 0);
    active_leases := (SELECT pg_catalog.count(*) FROM public.api_football_quota_leases l
                      WHERE l.api_key_hash OPERATOR(pg_catalog.=) p_key_hash
                        AND l.expires_at OPERATOR(pg_catalog.>) pg_catalog.clock_timestamp())::pg_catalog.int4;
    v_rolling := (SELECT pg_catalog.count(*) FROM public.api_football_quota_attempts a
                  WHERE a.api_key_hash OPERATOR(pg_catalog.=) p_key_hash
                    AND a.created_at OPERATOR(pg_catalog.>) (pg_catalog.clock_timestamp() OPERATOR(pg_catalog.-) '60 seconds'::pg_catalog.interval))::pg_catalog.int4;
    IF EXISTS (SELECT 1 FROM public.api_football_quota_attempts a
               WHERE a.api_key_hash OPERATOR(pg_catalog.=) p_key_hash
                 AND a.attempt_id OPERATOR(pg_catalog.~~) 'provider-zero-%'::pg_catalog.text
                 AND a.created_at OPERATOR(pg_catalog.>) (pg_catalog.clock_timestamp() OPERATOR(pg_catalog.-) '60 seconds'::pg_catalog.interval)) THEN
        v_rolling := 8;
    END IF;
    remaining_minute := GREATEST(0, LEAST(8 OPERATOR(pg_catalog.-) v_minute, 8 OPERATOR(pg_catalog.-) v_rolling));
    remaining_day := GREATEST(0, 80 OPERATOR(pg_catalog.-) v_day);
    RETURN NEXT;
END
$fcbcertsnapshot$;

CREATE OR REPLACE FUNCTION fcb_cert_quota.mark_exhausted(p_key_hash pg_catalog.text, p_kind pg_catalog.text)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fcbcertexhausted$
DECLARE
    v_epoch pg_catalog.int8;
    v_prefix pg_catalog.text;
BEGIN
    IF SESSION_USER OPERATOR(pg_catalog.<>) 'fcb_cert_runner'::pg_catalog.name THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_IDENTITY_REFUSED';
    END IF;
    IF pg_catalog.current_setting('transaction_isolation') OPERATOR(pg_catalog.<>) 'read committed'::pg_catalog.text THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_ISOLATION_REFUSED';
    END IF;
    IF p_kind IS NULL OR p_kind OPERATOR(pg_catalog.<>) ALL (ARRAY['minute', 'day']::pg_catalog.text[]) THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_BAD_WINDOW';
    END IF;
    IF NOT COALESCE(p_key_hash OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'::pg_catalog.text, false) THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_BAD_KEY_HASH';
    END IF;
    IF p_key_hash OPERATOR(pg_catalog.<>) fcb_cert_quota.pinned_key_hash() THEN
        RAISE EXCEPTION 'FCB_CERT_QUOTA_KEY_REFUSED';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        ((('x'::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.substr(p_key_hash, 1, 16))::pg_catalog.bit(64))::pg_catalog.int8)
        OPERATOR(pg_catalog.&) 9223372036854775807::pg_catalog.int8);
    -- bounded rows: the key's attempts older than the 60 s window are never read again
    DELETE FROM public.api_football_quota_attempts a
     WHERE a.api_key_hash OPERATOR(pg_catalog.=) p_key_hash
       AND a.created_at OPERATOR(pg_catalog.<=) (pg_catalog.clock_timestamp() OPERATOR(pg_catalog.-) '60 seconds'::pg_catalog.interval);
    -- a provider-zero marker of the key is still live: the minute is already exhausted, nothing to add
    IF p_kind OPERATOR(pg_catalog.=) 'minute'::pg_catalog.text
       AND EXISTS (SELECT 1 FROM public.api_football_quota_attempts a
                   WHERE a.api_key_hash OPERATOR(pg_catalog.=) p_key_hash
                     AND a.attempt_id OPERATOR(pg_catalog.~~) 'provider-zero-%'::pg_catalog.text
                     AND a.created_at OPERATOR(pg_catalog.>) (pg_catalog.clock_timestamp() OPERATOR(pg_catalog.-) '60 seconds'::pg_catalog.interval)) THEN
        RETURN;
    END IF;
    v_epoch := EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::pg_catalog.int8;
    -- only ever RAISES a counter (GREATEST), to the largest limit any configuration may hold
    INSERT INTO public.api_football_quota_counters AS c (api_key_hash, window_kind, bucket, used)
    VALUES (p_key_hash, p_kind,
            v_epoch OPERATOR(pg_catalog./) (CASE WHEN p_kind OPERATOR(pg_catalog.=) 'minute'::pg_catalog.text THEN 60 ELSE 86400 END)::pg_catalog.int8,
            CASE WHEN p_kind OPERATOR(pg_catalog.=) 'minute'::pg_catalog.text THEN 10000 ELSE 1000000 END)
    ON CONFLICT (api_key_hash, window_kind, bucket)
    DO UPDATE SET used = GREATEST(c.used, EXCLUDED.used);
    IF p_kind OPERATOR(pg_catalog.=) 'minute'::pg_catalog.text THEN
        -- provider-reported zero: rolling slots so independent workers remain blocked for 60 s
        v_prefix := 'provider-zero-'::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.rtrim(pg_catalog.translate(pg_catalog.encode(
            pg_catalog.uuid_send(pg_catalog.gen_random_uuid()), 'base64'::pg_catalog.text), '+/'::pg_catalog.text, '-_'::pg_catalog.text),
            '='::pg_catalog.text);
        INSERT INTO public.api_football_quota_attempts (attempt_id, api_key_hash)
        SELECT v_prefix OPERATOR(pg_catalog.||) '-'::pg_catalog.text OPERATOR(pg_catalog.||) g.n::pg_catalog.text, p_key_hash
        FROM pg_catalog.generate_series(1, 8) AS g(n)
        ON CONFLICT (attempt_id) DO NOTHING;
    END IF;
END
$fcbcertexhausted$;

REVOKE ALL ON FUNCTION fcb_cert_quota.reserve(pg_catalog.text), fcb_cert_quota.release(pg_catalog.text, pg_catalog.text),
    fcb_cert_quota.snapshot(pg_catalog.text), fcb_cert_quota.mark_exhausted(pg_catalog.text, pg_catalog.text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fcb_cert_quota.reserve(pg_catalog.text), fcb_cert_quota.release(pg_catalog.text, pg_catalog.text),
    fcb_cert_quota.snapshot(pg_catalog.text), fcb_cert_quota.mark_exhausted(pg_catalog.text, pg_catalog.text) TO fcb_cert_runner;
GRANT USAGE ON SCHEMA fcb_cert_quota TO fcb_cert_runner;
