-- FCB OAuth persistent throttle (owner decisions D01-D12, 2026-11-11).
-- Additive only: CREATE TABLE/INDEX IF NOT EXISTS. No DROP, ALTER, DELETE, UPDATE or data change.
--
-- oauth_throttle_buckets: one GCRA state row per (scope, key). key_hash is either '*' (the
-- global bucket of the scope) or '<key id>:<HMAC-SHA256 hex>' of the normalised client address
-- (IPv4, or the IPv6 /64), computed with a dedicated server secret: never a plain address.
-- tat is the GCRA "theoretical arrival time", written only from the database clock.
-- Rows are created only for requests that conform (denied keys leave no row) and are removed
-- by bounded opportunistic cleanup once tat is in the past (a decayed row equals no row).
CREATE TABLE IF NOT EXISTS oauth_throttle_buckets (
    scope TEXT NOT NULL CHECK (scope IN ('consent', 'registration')),
    key_hash TEXT NOT NULL CHECK (key_hash = '*' OR key_hash ~ '^[a-z0-9]{1,8}:[0-9a-f]{64}$'),
    tat TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope, key_hash)
);
CREATE INDEX IF NOT EXISTS idx_oauth_throttle_buckets_tat ON oauth_throttle_buckets (tat);

-- Emergency unlock evidence (D05, D11): the protected unlock workflow copies every bucket it
-- removes here, in the same transaction, before deleting it. The runtime role gets no privilege
-- on this table; nothing in the runtime reads or writes it.
CREATE TABLE IF NOT EXISTS oauth_throttle_unlock_audit (
    audit_id BIGSERIAL PRIMARY KEY,
    unlocked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    scope TEXT NOT NULL,
    target TEXT NOT NULL,
    github_run_id TEXT NOT NULL,
    github_actor TEXT NOT NULL,
    removed_key_hash TEXT,
    removed_tat TIMESTAMPTZ,
    removed_updated_at TIMESTAMPTZ
);
