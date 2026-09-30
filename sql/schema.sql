CREATE TABLE IF NOT EXISTS model_snapshots (
    match_id TEXT NOT NULL,
    snapshot_timestamp_utc TIMESTAMPTZ NOT NULL,
    kickoff_utc TIMESTAMPTZ NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    league TEXT NOT NULL DEFAULT '',
    source_match_id TEXT NOT NULL DEFAULT '',
    valid_prematch BOOLEAN NOT NULL DEFAULT FALSE,
    analysis_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    model_pick TEXT NOT NULL DEFAULT '',
    model_pick_probability DOUBLE PRECISION,
    picked_odds DOUBLE PRECISION,
    picked_edge DOUBLE PRECISION,
    pin_favorite TEXT NOT NULL DEFAULT '',
    pin_favorite_odds DOUBLE PRECISION,
    same_as_favorite BOOLEAN NOT NULL DEFAULT FALSE,
    sha256_model_raw TEXT NOT NULL DEFAULT '',
    sha256_odds_raw TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, snapshot_timestamp_utc)
);

CREATE INDEX IF NOT EXISTS idx_model_snapshots_kickoff ON model_snapshots (kickoff_utc);
CREATE INDEX IF NOT EXISTS idx_model_snapshots_match_kickoff ON model_snapshots (match_id, kickoff_utc);

CREATE TABLE IF NOT EXISTS match_results (
    match_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    result_1x2 TEXT NOT NULL DEFAULT '',
    final_home_goals INTEGER,
    final_away_goals INTEGER,
    result_timestamp_utc TIMESTAMPTZ,
    result_source TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, version)
);

CREATE INDEX IF NOT EXISTS idx_match_results_match ON match_results (match_id, version DESC);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    rows_seen INTEGER NOT NULL,
    rows_inserted INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, source_sha256)
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    resource TEXT NOT NULL,
    scope TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS oauth_access_tokens (
    token_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    scope TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    scope TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_oauth_access_active
ON oauth_access_tokens (expires_at)
WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_oauth_refresh_active
ON oauth_refresh_tokens (expires_at)
WHERE revoked_at IS NULL;


CREATE TABLE IF NOT EXISTS decision_batches (
    batch_id TEXT PRIMARY KEY,
    requested_at_utc TIMESTAMPTZ NOT NULL,
    requested_date DATE NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Europe/Rome',
    mode TEXT NOT NULL DEFAULT 'all',
    strategy_filter JSONB NOT NULL DEFAULT '[]'::jsonb,
    label TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decision_batches_date
ON decision_batches (requested_date, requested_at_utc);

CREATE TABLE IF NOT EXISTS decision_tickets (
    batch_id TEXT NOT NULL REFERENCES decision_batches(batch_id) ON DELETE RESTRICT,
    match_id TEXT NOT NULL,
    snapshot_timestamp_utc TIMESTAMPTZ NOT NULL,
    kickoff_utc TIMESTAMPTZ NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    league TEXT NOT NULL DEFAULT '',
    model_pick TEXT NOT NULL DEFAULT '',
    model_probability DOUBLE PRECISION,
    picked_odds DOUBLE PRECISION,
    picked_edge DOUBLE PRECISION,
    strategies JSONB NOT NULL DEFAULT '[]'::jsonb,
    display_status TEXT NOT NULL DEFAULT 'NO_BET',
    snapshot_view JSONB NOT NULL,
    shown_to_user BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (batch_id, match_id)
);

CREATE INDEX IF NOT EXISTS idx_decision_tickets_match
ON decision_tickets (match_id, kickoff_utc);


CREATE TABLE IF NOT EXISTS source_pilot_runs (
    run_id TEXT PRIMARY KEY,
    pilot_id TEXT NOT NULL,
    source TEXT NOT NULL,
    slot_no INTEGER NOT NULL,
    started_at_utc TIMESTAMPTZ NOT NULL,
    finished_at_utc TIMESTAMPTZ,
    status TEXT NOT NULL,
    final_url TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    browser_session_id TEXT NOT NULL DEFAULT '',
    browser_cost DOUBLE PRECISION,
    proxy_cost DOUBLE PRECISION,
    proxy_used_mb DOUBLE PRECISION,
    captcha_cost DOUBLE PRECISION,
    html_sha256 TEXT NOT NULL DEFAULT '',
    screenshot_sha256 TEXT NOT NULL DEFAULT '',
    html_gzip BYTEA,
    screenshot_png BYTEA,
    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (pilot_id, source, slot_no)
);

CREATE INDEX IF NOT EXISTS idx_source_pilot_runs_pilot
ON source_pilot_runs (pilot_id, source, slot_no);

CREATE TABLE IF NOT EXISTS source_pilot_predictions (
    run_id TEXT NOT NULL REFERENCES source_pilot_runs(run_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_match_key TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    league TEXT NOT NULL DEFAULT '',
    kickoff_raw TEXT NOT NULL DEFAULT '',
    source_timezone TEXT NOT NULL DEFAULT '',
    kickoff_utc TIMESTAMPTZ,
    timezone_status TEXT NOT NULL DEFAULT 'TIMEZONE_UNRESOLVED',
    p1 DOUBLE PRECISION,
    px DOUBLE PRECISION,
    p2 DOUBLE PRECISION,
    over25 DOUBLE PRECISION,
    under25 DOUBLE PRECISION,
    btts_yes DOUBLE PRECISION,
    btts_no DOUBLE PRECISION,
    xg_home DOUBLE PRECISION,
    xg_away DOUBLE PRECISION,
    correct_score TEXT NOT NULL DEFAULT '',
    parse_status TEXT NOT NULL DEFAULT 'OK',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, source_match_key)
);

CREATE INDEX IF NOT EXISTS idx_source_pilot_predictions_source
ON source_pilot_predictions (source, kickoff_utc);

CREATE TABLE IF NOT EXISTS source_pilot_matches (
    run_id TEXT NOT NULL REFERENCES source_pilot_runs(run_id) ON DELETE CASCADE,
    source_match_key TEXT NOT NULL,
    anchor_match_id TEXT,
    match_status TEXT NOT NULL,
    match_confidence DOUBLE PRECISION,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, source_match_key)
);

CREATE TABLE IF NOT EXISTS source_pilot_control (
    pilot_id TEXT PRIMARY KEY,
    started_at_utc TIMESTAMPTZ NOT NULL,
    ends_at_utc TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    slots_expected INTEGER NOT NULL DEFAULT 16,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS strategy_simulations (
    batch_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    kickoff_utc TIMESTAMPTZ NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    pick TEXT NOT NULL,
    probability DOUBLE PRECISION,
    odds DOUBLE PRECISION,
    edge DOUBLE PRECISION,
    stake_eur NUMERIC(12,2) NOT NULL DEFAULT 1.00,
    priced BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'PENDING',
    result_1x2 TEXT,
    correct BOOLEAN,
    profit_eur NUMERIC(14,4),
    source_snapshot_timestamp_utc TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (batch_id, match_id, strategy_id)
);

CREATE INDEX IF NOT EXISTS idx_strategy_simulations_strategy
ON strategy_simulations (strategy_id, status, kickoff_utc);

CREATE TABLE IF NOT EXISTS strategy_stats (
    strategy_id TEXT PRIMARY KEY,
    strategy_name TEXT NOT NULL,
    description TEXT NOT NULL,
    stake_eur NUMERIC(12,2) NOT NULL DEFAULT 1.00,
    signals_total INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    settled INTEGER NOT NULL DEFAULT 0,
    priced_settled INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    staked_eur NUMERIC(14,2) NOT NULL DEFAULT 0,
    profit_eur NUMERIC(14,4) NOT NULL DEFAULT 0,
    cumulative_profit_eur NUMERIC(14,4) NOT NULL DEFAULT 0,
    win_rate DOUBLE PRECISION,
    roi DOUBLE PRECISION,
    sample_reliability TEXT NOT NULL DEFAULT 'INSUFFICIENT_SAMPLE',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
