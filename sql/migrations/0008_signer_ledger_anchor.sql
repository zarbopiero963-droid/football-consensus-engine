-- SX Bet isolated signer: external anchor of the order-ledger head + writer lease (governance §2.6, §5).
-- Additive only. Used by signer/anchor.py:PostgresAnchorStore, never by the MCP bridge.
-- signer_ledger_anchors is APPEND-ONLY: one row per anchored ledger head, seq strictly increasing per
-- ledger, each row carrying the fencing token of the writer lease that wrote it. The database refuses
-- UPDATE, DELETE and TRUNCATE on it, a non-increasing seq, and a token other than the current lease's.
-- signer_ledger_writer_leases holds one row per ledger: the current fencing token (incremented on every
-- lease acquisition, under a session advisory lock held by the writer).
CREATE TABLE IF NOT EXISTS signer_ledger_writer_leases (
    ledger_id TEXT PRIMARY KEY CHECK (ledger_id ~ '^[A-Za-z0-9_.:-]{1,200}$'),
    fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
    holder TEXT NOT NULL CHECK (length(holder) <= 200),
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS signer_ledger_anchors (
    ledger_id TEXT NOT NULL CHECK (ledger_id ~ '^[A-Za-z0-9_.:-]{1,200}$'),
    seq BIGINT NOT NULL CHECK (seq > 0),
    head_hash TEXT NOT NULL CHECK (head_hash ~ '^sha256:[0-9a-f]{64}$'),
    fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (ledger_id, seq)
);
CREATE OR REPLACE FUNCTION signer_ledger_anchors_append_only() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'SIGNER_LEDGER_ANCHOR_APPEND_ONLY';
END;
$fn$;
CREATE OR REPLACE FUNCTION signer_ledger_anchors_guard_insert() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    last_seq BIGINT;
    lease_token BIGINT;
BEGIN
    SELECT max(a.seq) INTO last_seq FROM signer_ledger_anchors a WHERE a.ledger_id = NEW.ledger_id;
    IF last_seq IS NOT NULL AND NEW.seq <= last_seq THEN
        RAISE EXCEPTION 'SIGNER_LEDGER_ANCHOR_SEQ_NOT_MONOTONIC';
    END IF;
    SELECT l.fencing_token INTO lease_token FROM signer_ledger_writer_leases l WHERE l.ledger_id = NEW.ledger_id;
    IF lease_token IS NULL OR lease_token <> NEW.fencing_token THEN
        RAISE EXCEPTION 'SIGNER_LEDGER_ANCHOR_FENCED';
    END IF;
    RETURN NEW;
END;
$fn$;
CREATE OR REPLACE TRIGGER signer_ledger_anchors_no_mutation
    BEFORE UPDATE OR DELETE ON signer_ledger_anchors
    FOR EACH ROW EXECUTE FUNCTION signer_ledger_anchors_append_only();
CREATE OR REPLACE TRIGGER signer_ledger_anchors_no_truncate
    BEFORE TRUNCATE ON signer_ledger_anchors
    FOR EACH STATEMENT EXECUTE FUNCTION signer_ledger_anchors_append_only();
CREATE OR REPLACE TRIGGER signer_ledger_anchors_insert_guard
    BEFORE INSERT ON signer_ledger_anchors
    FOR EACH ROW EXECUTE FUNCTION signer_ledger_anchors_guard_insert();
