-- SX Bet isolated signer: persistent request queue between the MCP bridge and the signer (issue #187, S6).
-- DRAFT, NOT in the signer migration series yet: promoting it to sql/migrations/signer/ (and updating the
-- series tests) is a step coordinated with WP-009 and the owner. Tests apply it only to a throwaway database.
-- Signer series only (never the bridge database: WP-009 guards refuse signer schema there). Additive.
-- Both tables are APPEND-ONLY: the database refuses UPDATE, DELETE and TRUNCATE. A request is written once
-- by the MCP side; every state change is a new row in signer_order_request_events written by the signer.
-- No roles and no GRANT here: least-privilege roles (MCP: INSERT requests + SELECT events; signer: SELECT
-- requests + INSERT events) are an explicit owner step coordinated with WP-009, not part of this migration.
CREATE TABLE IF NOT EXISTS signer_order_requests (
    request_id TEXT PRIMARY KEY CHECK (request_id ~ '^[0-9a-f]{32}$'),
    kind TEXT NOT NULL CHECK (kind IN ('PLACE', 'CANCEL')),
    payload JSONB NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS signer_order_request_events (
    request_id TEXT NOT NULL REFERENCES signer_order_requests (request_id),
    seq INTEGER NOT NULL CHECK (seq > 0),
    state TEXT NOT NULL CHECK (state IN ('CLAIMED', 'REFUSED', 'SIGNED', 'SUBMITTED', 'UNKNOWN', 'DONE')),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    signer_id TEXT NOT NULL CHECK (length(signer_id) BETWEEN 1 AND 200),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (request_id, seq)
);
-- One claim per request: a second signer instance (or a restart) can never process the same request twice.
CREATE UNIQUE INDEX IF NOT EXISTS signer_order_request_events_one_claim
    ON signer_order_request_events (request_id) WHERE state = 'CLAIMED';
CREATE OR REPLACE FUNCTION signer_order_requests_append_only() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'SIGNER_ORDER_REQUESTS_APPEND_ONLY';
END;
$fn$;
CREATE OR REPLACE TRIGGER signer_order_requests_no_mutation
    BEFORE UPDATE OR DELETE ON signer_order_requests
    FOR EACH ROW EXECUTE FUNCTION signer_order_requests_append_only();
CREATE OR REPLACE TRIGGER signer_order_requests_no_truncate
    BEFORE TRUNCATE ON signer_order_requests
    FOR EACH STATEMENT EXECUTE FUNCTION signer_order_requests_append_only();
CREATE OR REPLACE TRIGGER signer_order_request_events_no_mutation
    BEFORE UPDATE OR DELETE ON signer_order_request_events
    FOR EACH ROW EXECUTE FUNCTION signer_order_requests_append_only();
CREATE OR REPLACE TRIGGER signer_order_request_events_no_truncate
    BEFORE TRUNCATE ON signer_order_request_events
    FOR EACH STATEMENT EXECUTE FUNCTION signer_order_requests_append_only();
-- No event after a terminal one (review 1a9e09e P2-3): REFUSED and DONE close a request for good.
-- These guards hold for writers that take the per-request advisory lock (PostgresRequestQueue does); two
-- concurrent writers that skip it can race past them: defence in depth only, the grants are the control.
CREATE OR REPLACE FUNCTION signer_order_request_events_guard_insert() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF EXISTS (SELECT 1 FROM signer_order_request_events e WHERE e.request_id = NEW.request_id
               AND e.state IN ('REFUSED', 'DONE')) THEN
        RAISE EXCEPTION 'SIGNER_ORDER_REQUEST_ALREADY_TERMINAL';
    END IF;
    IF NEW.state = 'CLAIMED' AND NEW.seq <> 1 THEN
        RAISE EXCEPTION 'SIGNER_ORDER_REQUEST_CLAIM_NOT_FIRST';
    END IF;
    IF NEW.state <> 'CLAIMED' AND NOT EXISTS (SELECT 1 FROM signer_order_request_events c
               WHERE c.request_id = NEW.request_id AND c.state = 'CLAIMED' AND c.signer_id = NEW.signer_id) THEN
        RAISE EXCEPTION 'SIGNER_ORDER_REQUEST_NOT_CLAIMED_BY_WRITER';
    END IF;
    RETURN NEW;
END;
$fn$;
CREATE OR REPLACE TRIGGER signer_order_request_events_insert_guard
    BEFORE INSERT ON signer_order_request_events
    FOR EACH ROW EXECUTE FUNCTION signer_order_request_events_guard_insert();
