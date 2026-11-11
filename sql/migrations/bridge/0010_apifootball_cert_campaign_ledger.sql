-- AF-07 (owner approval 2026-11-11, offline development): persistent campaign ledger for the
-- API-FOOTBALL certification campaigns (scripts/apifootball_cert_batch1.py). DEDICATED tables:
-- the shared quota coordinator (api_football_quota_*) is neither reused nor altered.
-- Additive only: new tables, functions and triggers. No DROP, ALTER, UPDATE, DELETE or data change
-- on any existing object. Depends on 0009 (PR #198) only through the consecutive numbering.
--
-- The bridge runtime never reads or writes these tables and never applies migrations; this one is
-- applied only by the owner-gated workflow fcb-aiven-defaultdb-apifootball-cert-ledger-0010.yml.
--
-- AF-15 (owner decision 2026-11-11): the budget belongs to an owner-APPROVED PLAN, not to a
-- campaign. Every campaign_id of one plan shares the plan's budget: a new campaign_id, a restart or
-- a new dispatch renews nothing. A plan row is written only by the owner-gated approval workflow
-- (fcb-aiven-defaultdb-apifootball-cert-plan-approve.yml, as fcb_migrator); the runtime role only
-- reads it. A plan never changes: a new budget needs a new plan (new sha256) and a new approval.

-- api_football_cert_plans: append-only register of approved plans. plan_sha256 is the SHA-256 of
-- the plan file committed on main (docs/certification/apifootball/batch1_plan.json or
-- live_snapshot_plan.json). approved_by / github_run_id come from the GitHub context of the
-- approval workflow; approved_role and approved_at are stamped by the server.
CREATE TABLE IF NOT EXISTS api_football_cert_plans (
    plan_sha256 TEXT PRIMARY KEY CHECK (plan_sha256 ~ '^[0-9a-f]{64}$'),
    plan_kind TEXT NOT NULL CHECK (plan_kind IN ('BATCH1', 'LIVE_FULL_MATCH_SNAPSHOT')),
    budget INTEGER NOT NULL CHECK (budget BETWEEN 1 AND 78),
    approved_by TEXT NOT NULL CHECK (approved_by ~ '^[A-Za-z0-9][A-Za-z0-9-]{0,38}$'),
    github_run_id BIGINT NOT NULL CHECK (github_run_id > 0),
    approved_role TEXT NOT NULL DEFAULT session_user,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (plan_kind <> 'LIVE_FULL_MATCH_SNAPSHOT' OR budget <= 8)
);

-- api_football_cert_campaigns: one row per campaign_id, bound to an approved plan at creation and
-- immutable afterwards.
CREATE TABLE IF NOT EXISTS api_football_cert_campaigns (
    campaign_id TEXT PRIMARY KEY CHECK (campaign_id ~ '^[a-z0-9][a-z0-9-]{7,63}$'),
    plan_sha256 TEXT NOT NULL REFERENCES api_football_cert_plans (plan_sha256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- api_football_cert_campaign_attempts: append-only, one row per provider HTTP attempt, written
-- (committed) BEFORE the request leaves (write-ahead). seq is dense 1.. per campaign; the number of
-- attempts of ALL the campaigns of a plan never exceeds the plan's budget.
CREATE TABLE IF NOT EXISTS api_football_cert_campaign_attempts (
    campaign_id TEXT NOT NULL REFERENCES api_football_cert_campaigns (campaign_id),
    seq INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 78),
    call_id TEXT NOT NULL CHECK (call_id ~ '^[A-Za-z0-9_-]{1,32}$'),
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (campaign_id, seq)
);

-- api_football_cert_campaign_outcomes: append-only, at most one outcome per reserved attempt,
-- written AFTER the provider exchange. A RESERVED attempt (row in _attempts) is not proof that a
-- request was SENT: an attempt without an outcome is RESERVED_UNKNOWN (the process may have stopped
-- before, during or after the send). It consumes budget and is never retried or recovered
-- automatically. TRANSPORT_ERROR is ambiguous too (the request may have reached the provider).
CREATE TABLE IF NOT EXISTS api_football_cert_campaign_outcomes (
    campaign_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('HTTP_RESPONSE', 'TRANSPORT_ERROR')),
    http_status INTEGER CHECK (http_status BETWEEN 100 AND 599),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (campaign_id, seq),
    FOREIGN KEY (campaign_id, seq) REFERENCES api_football_cert_campaign_attempts (campaign_id, seq),
    CHECK ((outcome = 'HTTP_RESPONSE') = (http_status IS NOT NULL))
);

-- Every 0010 function pins its search_path (pg_catalog first, the session's temporary schema
-- last) and reaches the ledger tables only through the schema of the table that fired it
-- (TG_TABLE_SCHEMA: `public` in production, a throwaway schema in tests). A caller can therefore
-- never redirect a check to an object of its own, e.g. a pg_temp.api_football_cert_plans or
-- pg_temp.api_football_cert_campaigns with a larger budget, or to a planted operator or function.

-- Rows never change and are never removed (plans, campaigns, attempts and outcomes alike). These
-- statement-level triggers refuse UPDATE, DELETE and TRUNCATE for EVERY role, the table owner
-- included; the runtime role holds no UPDATE/DELETE/TRUNCATE privilege either and, not being the
-- owner, cannot ALTER the tables or DISABLE these triggers.
CREATE FUNCTION api_football_cert_reject_mutation() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'API_FOOTBALL_CERT_LEDGER_APPEND_ONLY';
END;
$$;
CREATE TRIGGER api_football_cert_plans_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON api_football_cert_plans FOR EACH STATEMENT EXECUTE FUNCTION api_football_cert_reject_mutation();
CREATE TRIGGER api_football_cert_campaigns_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON api_football_cert_campaigns FOR EACH STATEMENT EXECUTE FUNCTION api_football_cert_reject_mutation();
CREATE TRIGGER api_football_cert_attempts_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON api_football_cert_campaign_attempts FOR EACH STATEMENT EXECUTE FUNCTION api_football_cert_reject_mutation();
CREATE TRIGGER api_football_cert_outcomes_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON api_football_cert_campaign_outcomes FOR EACH STATEMENT EXECUTE FUNCTION api_football_cert_reject_mutation();

-- Audit values are the server's, never the writer's: whatever an INSERT or COPY supplies,
-- approved_at / approved_role / created_at / reserved_at / recorded_at are overwritten here. (The
-- runtime role also has no INSERT privilege on these columns, nor on the plans table at all.)
CREATE FUNCTION api_football_cert_stamp() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF TG_TABLE_NAME OPERATOR(pg_catalog.=) 'api_football_cert_plans' THEN
        NEW.approved_at := pg_catalog.clock_timestamp();
        NEW.approved_role := SESSION_USER;
    ELSIF TG_TABLE_NAME OPERATOR(pg_catalog.=) 'api_football_cert_campaigns' THEN
        NEW.created_at := pg_catalog.clock_timestamp();
    ELSIF TG_TABLE_NAME OPERATOR(pg_catalog.=) 'api_football_cert_campaign_attempts' THEN
        NEW.reserved_at := pg_catalog.clock_timestamp();
    ELSIF TG_TABLE_NAME OPERATOR(pg_catalog.=) 'api_football_cert_campaign_outcomes' THEN
        NEW.recorded_at := pg_catalog.clock_timestamp();
    ELSE
        RAISE EXCEPTION 'API_FOOTBALL_CERT_STAMP_UNKNOWN_TABLE';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER api_football_cert_plans_stamp BEFORE INSERT ON api_football_cert_plans
    FOR EACH ROW EXECUTE FUNCTION api_football_cert_stamp();
CREATE TRIGGER api_football_cert_campaigns_stamp BEFORE INSERT ON api_football_cert_campaigns
    FOR EACH ROW EXECUTE FUNCTION api_football_cert_stamp();
CREATE TRIGGER api_football_cert_attempts_stamp BEFORE INSERT ON api_football_cert_campaign_attempts
    FOR EACH ROW EXECUTE FUNCTION api_football_cert_stamp();
CREATE TRIGGER api_football_cert_outcomes_stamp BEFORE INSERT ON api_football_cert_campaign_outcomes
    FOR EACH ROW EXECUTE FUNCTION api_football_cert_stamp();

-- The database enforces the PLAN budget: under a transaction-scoped advisory lock on the plan
-- (deterministic key: namespace 460244131, hashtext(plan_sha256); the runner takes the same lock),
-- the attempts of EVERY campaign of the plan are counted, RESERVED_UNKNOWN included, and an attempt
-- beyond the plan's budget is refused. seq must also be the campaign's next number; the primary key
-- (campaign_id, seq) is the backstop. A rolled-back reservation was never committed, hence never
-- sent, and does not count.
CREATE FUNCTION api_football_cert_attempt_guard() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    plan TEXT;
    cap INTEGER;
    used INTEGER;
    campaign_used INTEGER;
BEGIN
    EXECUTE pg_catalog.format('SELECT c.plan_sha256 FROM %I.api_football_cert_campaigns c '
                              'WHERE c.campaign_id OPERATOR(pg_catalog.=) $1', TG_TABLE_SCHEMA)
        INTO plan USING NEW.campaign_id;
    IF plan IS NULL THEN
        RAISE EXCEPTION 'API_FOOTBALL_CERT_CAMPAIGN_NOT_OPEN';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(460244131, pg_catalog.hashtext(plan));
    EXECUTE pg_catalog.format('SELECT p.budget FROM %I.api_football_cert_plans p '
                              'WHERE p.plan_sha256 OPERATOR(pg_catalog.=) $1', TG_TABLE_SCHEMA)
        INTO cap USING plan;
    IF cap IS NULL THEN
        RAISE EXCEPTION 'API_FOOTBALL_CERT_PLAN_NOT_APPROVED';
    END IF;
    EXECUTE pg_catalog.format('SELECT pg_catalog.count(*) FROM %I.api_football_cert_campaign_attempts a '
                              'JOIN %I.api_football_cert_campaigns c ON c.campaign_id OPERATOR(pg_catalog.=) a.campaign_id '
                              'WHERE c.plan_sha256 OPERATOR(pg_catalog.=) $1', TG_TABLE_SCHEMA, TG_TABLE_SCHEMA)
        INTO used USING plan;
    EXECUTE pg_catalog.format('SELECT pg_catalog.count(*) FROM %I.api_football_cert_campaign_attempts a '
                              'WHERE a.campaign_id OPERATOR(pg_catalog.=) $1', TG_TABLE_SCHEMA)
        INTO campaign_used USING NEW.campaign_id;
    IF (used OPERATOR(pg_catalog.>=) cap)
       OR (NEW.seq OPERATOR(pg_catalog.<>) (campaign_used OPERATOR(pg_catalog.+) 1)) THEN
        RAISE EXCEPTION 'API_FOOTBALL_CERT_CAMPAIGN_BUDGET_EXHAUSTED';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER api_football_cert_attempt_budget BEFORE INSERT ON api_football_cert_campaign_attempts
    FOR EACH ROW EXECUTE FUNCTION api_football_cert_attempt_guard();
