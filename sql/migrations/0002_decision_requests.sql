CREATE TABLE decision_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    batch_id TEXT NOT NULL UNIQUE REFERENCES decision_batches(batch_id) ON DELETE RESTRICT,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
