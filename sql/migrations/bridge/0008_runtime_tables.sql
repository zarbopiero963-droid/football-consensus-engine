-- FCB-WP-004: versioned home of the runtime-only tables (CLAUDE.md §5).
-- Until now these were created only by the running code with CREATE TABLE IF NOT EXISTS.
-- The DDL is byte-for-byte the runtime one, so this migration is a no-op on a database
-- where the runtime already created them, and the runtime stays a no-op after it.
-- Additive only: no DROP, ALTER, DELETE, UPDATE or data change.

-- Shared API-FOOTBALL quota coordinator (bridge/api_football/quota.py _SCHEMA).
CREATE TABLE IF NOT EXISTS api_football_quota_counters (
         api_key_hash TEXT NOT NULL,
         window_kind TEXT NOT NULL,
         bucket BIGINT NOT NULL,
         used INTEGER NOT NULL,
         PRIMARY KEY (api_key_hash, window_kind, bucket)
       );
CREATE TABLE IF NOT EXISTS api_football_quota_leases (
         lease_id TEXT PRIMARY KEY,
         api_key_hash TEXT NOT NULL,
         expires_at TIMESTAMPTZ NOT NULL
       );
CREATE INDEX IF NOT EXISTS idx_api_football_lease_expires
       ON api_football_quota_leases(api_key_hash, expires_at);
CREATE TABLE IF NOT EXISTS api_football_quota_attempts (
         attempt_id TEXT PRIMARY KEY,
         api_key_hash TEXT NOT NULL,
         created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
       );
CREATE INDEX IF NOT EXISTS idx_api_football_attempt_recent
       ON api_football_quota_attempts(api_key_hash, created_at);

-- OAuth dynamic client registration for Claude (bridge/oauth_store.py register_claude_dcr_client).
CREATE TABLE IF NOT EXISTS oauth_claude_dcr_clients (
               client_id_hash TEXT PRIMARY KEY,
               redirect_uri TEXT NOT NULL,
               created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
               expires_at TIMESTAMPTZ NOT NULL
            );
