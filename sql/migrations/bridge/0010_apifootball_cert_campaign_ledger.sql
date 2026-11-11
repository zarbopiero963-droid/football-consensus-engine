-- AF-07 (owner approval 2026-11-11, offline development): persistent campaign ledger for the
-- API-FOOTBALL certification campaigns (scripts/apifootball_cert_batch1.py). DEDICATED tables:
-- the shared quota coordinator (api_football_quota_*) is neither reused nor altered.
-- Additive only: new tables, functions and triggers. No DROP, ALTER, UPDATE, DELETE or data change
-- on any existing object. Depends on 0009 (PR #198) only through the consecutive numbering.
--
-- The bridge runtime never reads or writes these tables and never applies migrations; this one is
-- applied only by the owner-gated workflow fcb-aiven-defaultdb-apifootball-cert-ledger-0010.yml.
--
-- api_football_cert_campaigns: one row per campaign_id, budget fixed at creation (1..78),
-- immutable afterwards (no reset after a restart or a new dispatch).
CREATE TABLE IF NOT EXISTS api_football_cert_campaigns (
    campaign_id TEXT PRIMARY KEY CHECK (campaign_id ~ '^[a-z0-9][a-z0-9-]{7,63}$'),
    budget INTEGER NOT NULL CHECK (budget BETWEEN 1 AND 78),
    plan_sha256 TEXT NOT NULL CHECK (plan_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- api_football_cert_campaign_attempts: append-only, one row per provider HTTP attempt, written
-- (committed) BEFORE the request leaves (write-ahead). seq is dense 1..budget per campaign.
CREATE TABLE IF NOT EXISTS api_football_cert_campaign_attempts (
    campaign_id TEXT NOT NULL REFERENCES api_football_cert_campaigns (campaign_id),
    seq INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 78),
    call_id TEXT NOT NULL CHECK (call_id ~ '^[A-Za-z0-9_-]{1,32}$'),
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (campaign_id, seq)
);

-- Rows never change and are never removed (campaign budget and attempts alike).
CREATE FUNCTION api_football_cert_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'API_FOOTBALL_CERT_LEDGER_APPEND_ONLY';
END;
$$;
CREATE TRIGGER api_football_cert_campaigns_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON api_football_cert_campaigns FOR EACH STATEMENT EXECUTE FUNCTION api_football_cert_reject_mutation();
CREATE TRIGGER api_football_cert_attempts_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON api_football_cert_campaign_attempts FOR EACH STATEMENT EXECUTE FUNCTION api_football_cert_reject_mutation();

-- The database enforces the budget too: seq must be the next number and within the campaign's
-- budget. Concurrent reservations of one campaign are serialized by the runner's transaction-scoped
-- advisory lock; the primary key (campaign_id, seq) is the backstop (a racing duplicate seq fails).
CREATE FUNCTION api_football_cert_attempt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    cap INTEGER;
    used INTEGER;
BEGIN
    SELECT c.budget INTO cap FROM api_football_cert_campaigns c
        WHERE c.campaign_id = NEW.campaign_id;
    IF cap IS NULL THEN
        RAISE EXCEPTION 'API_FOOTBALL_CERT_CAMPAIGN_NOT_OPEN';
    END IF;
    SELECT count(*) INTO used FROM api_football_cert_campaign_attempts a WHERE a.campaign_id = NEW.campaign_id;
    IF used >= cap OR NEW.seq <> used + 1 THEN
        RAISE EXCEPTION 'API_FOOTBALL_CERT_CAMPAIGN_BUDGET_EXHAUSTED';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER api_football_cert_attempt_budget BEFORE INSERT ON api_football_cert_campaign_attempts
    FOR EACH ROW EXECUTE FUNCTION api_football_cert_attempt_guard();
