-- SX Bet shared rate coordinator (governance §2.4). Additive only.
-- Source of truth for bridge/sxbet/ratelimit.py:_SCHEMA, which repeats these
-- statements verbatim so a process can ensure them idempotently (IF NOT EXISTS
-- makes either order safe). Bucket keys hold a SHA-256 of the API key, never the key.
CREATE TABLE IF NOT EXISTS sxbet_rate_attempts (
    attempt_id TEXT PRIMARY KEY,
    bucket_key TEXT NOT NULL CHECK (bucket_key LIKE 'sxbet:v1:%' AND length(bucket_key) <= 256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_sxbet_rate_attempts_bucket
ON sxbet_rate_attempts(bucket_key, created_at);
CREATE INDEX IF NOT EXISTS idx_sxbet_rate_attempts_created
ON sxbet_rate_attempts(created_at);
CREATE TABLE IF NOT EXISTS sxbet_rate_cooldowns (
    bucket_key TEXT PRIMARY KEY CHECK (bucket_key LIKE 'sxbet:v1:%' AND length(bucket_key) <= 256),
    cooldown_until TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
