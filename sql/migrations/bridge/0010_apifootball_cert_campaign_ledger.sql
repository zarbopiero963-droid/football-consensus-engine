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

-- api_football_cert_plan_usage (review #199 P1, owner decision): the persistent per-plan counter
-- of committed reservations. One row per approved plan (created with used = 0 when the plan is
-- approved); used only grows, by exactly 1, and only through the SECURITY DEFINER budget trigger
-- below, in the SAME transaction as the attempt it numbers: a rollback or a killed process undoes
-- both. The runtime role has NO privilege on it. The guarantee does not depend on the isolation
-- level: in READ COMMITTED a concurrent increment waits for the row lock and re-checks
-- `used < budget` on the committed value; in REPEATABLE READ / SERIALIZABLE it fails with a
-- serialization error (40001) and nothing is reserved.
CREATE TABLE IF NOT EXISTS api_football_cert_plan_usage (
    plan_sha256 TEXT PRIMARY KEY REFERENCES api_football_cert_plans (plan_sha256),
    used INTEGER NOT NULL DEFAULT 0 CHECK (used BETWEEN 0 AND 78)
);

-- api_football_cert_campaigns: one row per campaign_id, bound to an approved plan at creation and
-- immutable afterwards.
CREATE TABLE IF NOT EXISTS api_football_cert_campaigns (
    campaign_id TEXT PRIMARY KEY CHECK (campaign_id ~ '^[a-z0-9][a-z0-9-]{7,63}$'),
    plan_sha256 TEXT NOT NULL REFERENCES api_football_cert_plans (plan_sha256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- api_football_cert_campaign_attempts: append-only, one row per provider HTTP attempt, written
-- (committed) BEFORE the request leaves (write-ahead). seq is assigned by the server: it is the
-- plan counter's value after the increment, so the seqs of all the campaigns of a plan are exactly
-- 1..used, never reused, never above the plan's budget.
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
-- owner, cannot ALTER the tables or DISABLE these triggers. The plan counter refuses DELETE and
-- TRUNCATE the same way, and any UPDATE other than "+1 below the budget" (row trigger below).
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
CREATE TRIGGER api_football_cert_plan_usage_kept BEFORE DELETE OR TRUNCATE
    ON api_football_cert_plan_usage FOR EACH STATEMENT EXECUTE FUNCTION api_football_cert_reject_mutation();

-- The counter: created at 0 with its plan; afterwards only ever +1, never above the plan's budget.
CREATE FUNCTION api_football_cert_plan_usage_rule() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    cap INTEGER;
BEGIN
    IF TG_TABLE_NAME OPERATOR(pg_catalog.=) 'api_football_cert_plans' THEN
        EXECUTE pg_catalog.format('INSERT INTO %I.api_football_cert_plan_usage (plan_sha256, used) VALUES ($1, 0)',
                                  TG_TABLE_SCHEMA) USING NEW.plan_sha256;
        RETURN NULL;
    END IF;
    EXECUTE pg_catalog.format('SELECT p.budget FROM %I.api_football_cert_plans p '
                              'WHERE p.plan_sha256 OPERATOR(pg_catalog.=) $1', TG_TABLE_SCHEMA)
        INTO cap USING NEW.plan_sha256;
    IF TG_OP OPERATOR(pg_catalog.=) 'INSERT' THEN
        IF cap IS NULL OR NEW.used OPERATOR(pg_catalog.<>) 0 THEN
            RAISE EXCEPTION 'API_FOOTBALL_CERT_PLAN_USAGE_RULE';
        END IF;
    ELSIF NEW.plan_sha256 OPERATOR(pg_catalog.<>) OLD.plan_sha256
          OR NEW.used OPERATOR(pg_catalog.<>) (OLD.used OPERATOR(pg_catalog.+) 1)
          OR NEW.used OPERATOR(pg_catalog.>) cap THEN
        RAISE EXCEPTION 'API_FOOTBALL_CERT_PLAN_USAGE_RULE';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER api_football_cert_plans_usage_init AFTER INSERT ON api_football_cert_plans
    FOR EACH ROW EXECUTE FUNCTION api_football_cert_plan_usage_rule();
CREATE TRIGGER api_football_cert_plan_usage_monotonic BEFORE INSERT OR UPDATE ON api_football_cert_plan_usage
    FOR EACH ROW EXECUTE FUNCTION api_football_cert_plan_usage_rule();

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

-- The database enforces the PLAN budget, independently of the isolation level (review #199 P1,
-- owner decision): in the SAME transaction as the attempt, the plan counter is incremented with
--   UPDATE ... SET used = used + 1 WHERE plan_sha256 = $1 AND used < budget RETURNING used
-- and the attempt's seq is that value; no row updated -> refused (BUDGET_EXHAUSTED when the counter
-- has reached the budget, PLAN_NOT_APPROVED when the plan has no counter). The function is
-- SECURITY DEFINER (owner: the ledger owner, fcb_migrator in production) because the runtime role
-- has no privilege on the counter; search_path is pinned (pg_catalog, then pg_temp LAST, as the
-- PostgreSQL documentation prescribes for SECURITY DEFINER) and every table is qualified with the
-- schema of the firing table. It refuses to run for anything but a BEFORE INSERT row trigger on an
-- api_football_cert_campaign_attempts table owned by its own owner (GUARD_MISUSE), so it cannot be
-- borrowed by another trigger (EXECUTE is also revoked from PUBLIC by the grant files). Whatever
-- seq a writer supplies is overwritten. RESERVED_UNKNOWN attempts are committed rows: they count.
CREATE FUNCTION api_football_cert_attempt_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    plan TEXT;
    numbered INTEGER;
BEGIN
    IF TG_OP OPERATOR(pg_catalog.<>) 'INSERT' OR TG_LEVEL OPERATOR(pg_catalog.<>) 'ROW'
       OR TG_WHEN OPERATOR(pg_catalog.<>) 'BEFORE'
       OR TG_TABLE_NAME OPERATOR(pg_catalog.<>) 'api_football_cert_campaign_attempts'
       OR (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid OPERATOR(pg_catalog.=) TG_RELID)
          OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) CURRENT_USER) THEN
        RAISE EXCEPTION 'API_FOOTBALL_CERT_GUARD_MISUSE';
    END IF;
    EXECUTE pg_catalog.format('SELECT c.plan_sha256 FROM %I.api_football_cert_campaigns c '
                              'WHERE c.campaign_id OPERATOR(pg_catalog.=) $1', TG_TABLE_SCHEMA)
        INTO plan USING NEW.campaign_id;
    IF plan IS NULL THEN
        RAISE EXCEPTION 'API_FOOTBALL_CERT_CAMPAIGN_NOT_OPEN';
    END IF;
    EXECUTE pg_catalog.format('UPDATE %I.api_football_cert_plan_usage u SET used = u.used OPERATOR(pg_catalog.+) 1 '
                              'FROM %I.api_football_cert_plans p '
                              'WHERE u.plan_sha256 OPERATOR(pg_catalog.=) $1 AND p.plan_sha256 OPERATOR(pg_catalog.=) u.plan_sha256 '
                              'AND u.used OPERATOR(pg_catalog.<) p.budget RETURNING u.used', TG_TABLE_SCHEMA, TG_TABLE_SCHEMA)
        INTO numbered USING plan;
    IF numbered IS NULL THEN
        EXECUTE pg_catalog.format('SELECT u.used FROM %I.api_football_cert_plan_usage u '
                                  'WHERE u.plan_sha256 OPERATOR(pg_catalog.=) $1', TG_TABLE_SCHEMA)
            INTO numbered USING plan;
        IF numbered IS NULL THEN
            RAISE EXCEPTION 'API_FOOTBALL_CERT_PLAN_NOT_APPROVED';
        END IF;
        RAISE EXCEPTION 'API_FOOTBALL_CERT_CAMPAIGN_BUDGET_EXHAUSTED';
    END IF;
    NEW.seq := numbered;
    RETURN NEW;
END;
$$;
CREATE TRIGGER api_football_cert_attempt_budget BEFORE INSERT ON api_football_cert_campaign_attempts
    FOR EACH ROW EXECUTE FUNCTION api_football_cert_attempt_guard();
