-- FCB migrations 0001-0007 only, regenerated from main
SELECT pg_advisory_xact_lock(460244120);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
DO $guard$ BEGIN
        IF EXISTS (SELECT 1 FROM schema_migrations WHERE version NOT IN (1,2,3,4,5,6,7)) THEN
            RAISE EXCEPTION 'Database has migrations absent from this checkout';
        END IF;
        END; $guard$;
DO '
    DECLARE applied schema_migrations%ROWTYPE;
    BEGIN
        SELECT * INTO applied FROM schema_migrations WHERE version=1;
        IF FOUND THEN
            IF applied.name <> ''0001_baseline.sql'' OR applied.sha256 <> ''a6cd3bda6f1bfb8f029d4039b2c50d0ed8d444bc12470b28a382faac068f7287'' THEN
                RAISE EXCEPTION ''Migration 1 checksum/name drift'';
            END IF;
        ELSE
            EXECUTE ''CREATE TABLE IF NOT EXISTS model_snapshots (
    match_id TEXT NOT NULL,
    snapshot_timestamp_utc TIMESTAMPTZ NOT NULL,
    kickoff_utc TIMESTAMPTZ NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    league TEXT NOT NULL DEFAULT '''''''',
    source_match_id TEXT NOT NULL DEFAULT '''''''',
    valid_prematch BOOLEAN NOT NULL DEFAULT FALSE,
    analysis_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    model_pick TEXT NOT NULL DEFAULT '''''''',
    model_pick_probability DOUBLE PRECISION,
    picked_odds DOUBLE PRECISION,
    picked_edge DOUBLE PRECISION,
    pin_favorite TEXT NOT NULL DEFAULT '''''''',
    pin_favorite_odds DOUBLE PRECISION,
    same_as_favorite BOOLEAN NOT NULL DEFAULT FALSE,
    sha256_model_raw TEXT NOT NULL DEFAULT '''''''',
    sha256_odds_raw TEXT NOT NULL DEFAULT '''''''',
    payload JSONB NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, snapshot_timestamp_utc)
);

CREATE INDEX IF NOT EXISTS idx_model_snapshots_kickoff ON model_snapshots (kickoff_utc);
CREATE INDEX IF NOT EXISTS idx_model_snapshots_match_kickoff ON model_snapshots (match_id, kickoff_utc);

CREATE TABLE IF NOT EXISTS match_results (
    match_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT '''''''',
    result_1x2 TEXT NOT NULL DEFAULT '''''''',
    final_home_goals INTEGER,
    final_away_goals INTEGER,
    result_timestamp_utc TIMESTAMPTZ,
    result_source TEXT NOT NULL DEFAULT '''''''',
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
    timezone TEXT NOT NULL DEFAULT ''''Europe/Rome'''',
    mode TEXT NOT NULL DEFAULT ''''all'''',
    strategy_filter JSONB NOT NULL DEFAULT ''''[]''''::jsonb,
    label TEXT NOT NULL DEFAULT '''''''',
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
    league TEXT NOT NULL DEFAULT '''''''',
    model_pick TEXT NOT NULL DEFAULT '''''''',
    model_probability DOUBLE PRECISION,
    picked_odds DOUBLE PRECISION,
    picked_edge DOUBLE PRECISION,
    strategies JSONB NOT NULL DEFAULT ''''[]''''::jsonb,
    display_status TEXT NOT NULL DEFAULT ''''NO_BET'''',
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
    final_url TEXT NOT NULL DEFAULT '''''''',
    http_status INTEGER,
    browser_session_id TEXT NOT NULL DEFAULT '''''''',
    browser_cost DOUBLE PRECISION,
    proxy_cost DOUBLE PRECISION,
    proxy_used_mb DOUBLE PRECISION,
    captcha_cost DOUBLE PRECISION,
    html_sha256 TEXT NOT NULL DEFAULT '''''''',
    screenshot_sha256 TEXT NOT NULL DEFAULT '''''''',
    html_gzip BYTEA,
    screenshot_png BYTEA,
    manifest JSONB NOT NULL DEFAULT ''''{}''''::jsonb,
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
    league TEXT NOT NULL DEFAULT '''''''',
    kickoff_raw TEXT NOT NULL DEFAULT '''''''',
    source_timezone TEXT NOT NULL DEFAULT '''''''',
    kickoff_utc TIMESTAMPTZ,
    timezone_status TEXT NOT NULL DEFAULT ''''TIMEZONE_UNRESOLVED'''',
    p1 DOUBLE PRECISION,
    px DOUBLE PRECISION,
    p2 DOUBLE PRECISION,
    over25 DOUBLE PRECISION,
    under25 DOUBLE PRECISION,
    btts_yes DOUBLE PRECISION,
    btts_no DOUBLE PRECISION,
    xg_home DOUBLE PRECISION,
    xg_away DOUBLE PRECISION,
    correct_score TEXT NOT NULL DEFAULT '''''''',
    parse_status TEXT NOT NULL DEFAULT ''''OK'''',
    payload JSONB NOT NULL DEFAULT ''''{}''''::jsonb,
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
    reason TEXT NOT NULL DEFAULT '''''''',
    PRIMARY KEY (run_id, source_match_key)
);

CREATE TABLE IF NOT EXISTS source_pilot_control (
    pilot_id TEXT PRIMARY KEY,
    started_at_utc TIMESTAMPTZ NOT NULL,
    ends_at_utc TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT ''''RUNNING'''',
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
    status TEXT NOT NULL DEFAULT ''''PENDING'''',
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
    sample_reliability TEXT NOT NULL DEFAULT ''''INSUFFICIENT_SAMPLE'''',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS market_decision_tickets (
    ticket_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES decision_batches(batch_id) ON DELETE RESTRICT,
    match_id TEXT NOT NULL,
    kickoff_utc TIMESTAMPTZ NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    league TEXT NOT NULL DEFAULT '''''''',
    source TEXT NOT NULL,
    market_id TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT ''''FT'''',
    selection TEXT NOT NULL,
    line DOUBLE PRECISION,
    probability DOUBLE PRECISION,
    odds DOUBLE PRECISION,
    edge DOUBLE PRECISION,
    derivation TEXT NOT NULL DEFAULT ''''SOURCE'''',
    source_snapshot_utc TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT ''''PENDING'''',
    result_value TEXT,
    correct BOOLEAN,
    stake_eur NUMERIC(12,2) NOT NULL DEFAULT 1.00,
    profit_eur NUMERIC(14,4),
    payload JSONB NOT NULL DEFAULT ''''{}''''::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(batch_id,match_id,market_id,source)
);

CREATE INDEX IF NOT EXISTS idx_market_decision_tickets_batch
ON market_decision_tickets(batch_id,kickoff_utc);

CREATE INDEX IF NOT EXISTS idx_market_decision_tickets_market
ON market_decision_tickets(market_id,source,status);

CREATE TABLE IF NOT EXISTS market_stats (
    market_id TEXT NOT NULL,
    source TEXT NOT NULL,
    signals_total INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    settled INTEGER NOT NULL DEFAULT 0,
    priced_settled INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    staked_eur NUMERIC(14,2) NOT NULL DEFAULT 0,
    profit_eur NUMERIC(14,4) NOT NULL DEFAULT 0,
    win_rate DOUBLE PRECISION,
    roi DOUBLE PRECISION,
    sample_reliability TEXT NOT NULL DEFAULT ''''INSUFFICIENT_SAMPLE'''',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(market_id,source)
);


CREATE TABLE IF NOT EXISTS result_observations (
    observation_id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_match_id TEXT NOT NULL DEFAULT '''''''',
    observed_at_utc TIMESTAMPTZ NOT NULL,
    kickoff_utc TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT '''''''',
    final_home_goals INTEGER,
    final_away_goals INTEGER,
    ht_home_goals INTEGER,
    ht_away_goals INTEGER,
    corners_home INTEGER,
    corners_away INTEGER,
    ht_corners_home INTEGER,
    ht_corners_away INTEGER,
    raw_sha256 TEXT NOT NULL DEFAULT '''''''',
    payload JSONB NOT NULL DEFAULT ''''{}''''::jsonb,
    UNIQUE(match_id,provider,observed_at_utc)
);

CREATE INDEX IF NOT EXISTS idx_result_observations_match
ON result_observations(match_id,provider,observed_at_utc DESC);

CREATE TABLE IF NOT EXISTS result_consensus (
    match_id TEXT PRIMARY KEY,
    final_home_goals INTEGER,
    final_away_goals INTEGER,
    ht_home_goals INTEGER,
    ht_away_goals INTEGER,
    corners_home INTEGER,
    corners_away INTEGER,
    ht_corners_home INTEGER,
    ht_corners_away INTEGER,
    result_1x2 TEXT,
    verification_status TEXT NOT NULL DEFAULT ''''UNVERIFIED'''',
    field_verification JSONB NOT NULL DEFAULT ''''{}''''::jsonb,
    providers JSONB NOT NULL DEFAULT ''''[]''''::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS result_source_pilot_control (
    pilot_id TEXT PRIMARY KEY,
    started_at_utc TIMESTAMPTZ NOT NULL,
    ends_at_utc TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT ''''RUNNING'''',
    slots_expected INTEGER NOT NULL DEFAULT 16,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS result_source_pilot_runs (
    run_id TEXT PRIMARY KEY,
    pilot_id TEXT NOT NULL,
    source TEXT NOT NULL,
    slot_no INTEGER NOT NULL,
    started_at_utc TIMESTAMPTZ NOT NULL,
    finished_at_utc TIMESTAMPTZ,
    status TEXT NOT NULL,
    final_url TEXT NOT NULL DEFAULT '''''''',
    http_status INTEGER,
    browser_session_id TEXT NOT NULL DEFAULT '''''''',
    browser_cost DOUBLE PRECISION,
    proxy_cost DOUBLE PRECISION,
    proxy_used_mb DOUBLE PRECISION,
    captcha_cost DOUBLE PRECISION,
    rows_found INTEGER NOT NULL DEFAULT 0,
    capabilities JSONB NOT NULL DEFAULT ''''{}''''::jsonb,
    html_sha256 TEXT NOT NULL DEFAULT '''''''',
    screenshot_sha256 TEXT NOT NULL DEFAULT '''''''',
    html_gzip BYTEA,
    screenshot_png BYTEA,
    manifest JSONB NOT NULL DEFAULT ''''{}''''::jsonb,
    UNIQUE (pilot_id, source, slot_no)
);

CREATE INDEX IF NOT EXISTS idx_result_source_pilot_runs
ON result_source_pilot_runs (pilot_id, source, slot_no);

-- Refuse to certify a pre-existing schema that lacks required baseline columns.
DO $baseline_check$ BEGIN
    IF EXISTS (
        SELECT 1 FROM (VALUES
        (''''model_snapshots'''',''''match_id''''),
        (''''model_snapshots'''',''''snapshot_timestamp_utc''''),
        (''''model_snapshots'''',''''kickoff_utc''''),
        (''''model_snapshots'''',''''home_team''''),
        (''''model_snapshots'''',''''away_team''''),
        (''''model_snapshots'''',''''league''''),
        (''''model_snapshots'''',''''source_match_id''''),
        (''''model_snapshots'''',''''valid_prematch''''),
        (''''model_snapshots'''',''''analysis_eligible''''),
        (''''model_snapshots'''',''''model_pick''''),
        (''''model_snapshots'''',''''model_pick_probability''''),
        (''''model_snapshots'''',''''picked_odds''''),
        (''''model_snapshots'''',''''picked_edge''''),
        (''''model_snapshots'''',''''pin_favorite''''),
        (''''model_snapshots'''',''''pin_favorite_odds''''),
        (''''model_snapshots'''',''''same_as_favorite''''),
        (''''model_snapshots'''',''''sha256_model_raw''''),
        (''''model_snapshots'''',''''sha256_odds_raw''''),
        (''''model_snapshots'''',''''payload''''),
        (''''model_snapshots'''',''''imported_at''''),
        (''''match_results'''',''''match_id''''),
        (''''match_results'''',''''version''''),
        (''''match_results'''',''''status''''),
        (''''match_results'''',''''result_1x2''''),
        (''''match_results'''',''''final_home_goals''''),
        (''''match_results'''',''''final_away_goals''''),
        (''''match_results'''',''''result_timestamp_utc''''),
        (''''match_results'''',''''result_source''''),
        (''''match_results'''',''''payload''''),
        (''''match_results'''',''''imported_at''''),
        (''''ingest_runs'''',''''id''''),
        (''''ingest_runs'''',''''source''''),
        (''''ingest_runs'''',''''source_sha256''''),
        (''''ingest_runs'''',''''rows_seen''''),
        (''''ingest_runs'''',''''rows_inserted''''),
        (''''ingest_runs'''',''''created_at''''),
        (''''runtime_state'''',''''key''''),
        (''''runtime_state'''',''''value''''),
        (''''runtime_state'''',''''updated_at''''),
        (''''oauth_authorization_codes'''',''''code_hash''''),
        (''''oauth_authorization_codes'''',''''client_id''''),
        (''''oauth_authorization_codes'''',''''redirect_uri''''),
        (''''oauth_authorization_codes'''',''''code_challenge''''),
        (''''oauth_authorization_codes'''',''''resource''''),
        (''''oauth_authorization_codes'''',''''scope''''),
        (''''oauth_authorization_codes'''',''''expires_at''''),
        (''''oauth_authorization_codes'''',''''used_at''''),
        (''''oauth_access_tokens'''',''''token_hash''''),
        (''''oauth_access_tokens'''',''''client_id''''),
        (''''oauth_access_tokens'''',''''resource''''),
        (''''oauth_access_tokens'''',''''scope''''),
        (''''oauth_access_tokens'''',''''expires_at''''),
        (''''oauth_access_tokens'''',''''revoked_at''''),
        (''''oauth_access_tokens'''',''''created_at''''),
        (''''oauth_refresh_tokens'''',''''token_hash''''),
        (''''oauth_refresh_tokens'''',''''client_id''''),
        (''''oauth_refresh_tokens'''',''''resource''''),
        (''''oauth_refresh_tokens'''',''''scope''''),
        (''''oauth_refresh_tokens'''',''''expires_at''''),
        (''''oauth_refresh_tokens'''',''''revoked_at''''),
        (''''oauth_refresh_tokens'''',''''created_at''''),
        (''''decision_batches'''',''''batch_id''''),
        (''''decision_batches'''',''''requested_at_utc''''),
        (''''decision_batches'''',''''requested_date''''),
        (''''decision_batches'''',''''timezone''''),
        (''''decision_batches'''',''''mode''''),
        (''''decision_batches'''',''''strategy_filter''''),
        (''''decision_batches'''',''''label''''),
        (''''decision_batches'''',''''created_at''''),
        (''''decision_tickets'''',''''batch_id''''),
        (''''decision_tickets'''',''''match_id''''),
        (''''decision_tickets'''',''''snapshot_timestamp_utc''''),
        (''''decision_tickets'''',''''kickoff_utc''''),
        (''''decision_tickets'''',''''home_team''''),
        (''''decision_tickets'''',''''away_team''''),
        (''''decision_tickets'''',''''league''''),
        (''''decision_tickets'''',''''model_pick''''),
        (''''decision_tickets'''',''''model_probability''''),
        (''''decision_tickets'''',''''picked_odds''''),
        (''''decision_tickets'''',''''picked_edge''''),
        (''''decision_tickets'''',''''strategies''''),
        (''''decision_tickets'''',''''display_status''''),
        (''''decision_tickets'''',''''snapshot_view''''),
        (''''decision_tickets'''',''''shown_to_user''''),
        (''''decision_tickets'''',''''created_at''''),
        (''''source_pilot_runs'''',''''run_id''''),
        (''''source_pilot_runs'''',''''pilot_id''''),
        (''''source_pilot_runs'''',''''source''''),
        (''''source_pilot_runs'''',''''slot_no''''),
        (''''source_pilot_runs'''',''''started_at_utc''''),
        (''''source_pilot_runs'''',''''finished_at_utc''''),
        (''''source_pilot_runs'''',''''status''''),
        (''''source_pilot_runs'''',''''final_url''''),
        (''''source_pilot_runs'''',''''http_status''''),
        (''''source_pilot_runs'''',''''browser_session_id''''),
        (''''source_pilot_runs'''',''''browser_cost''''),
        (''''source_pilot_runs'''',''''proxy_cost''''),
        (''''source_pilot_runs'''',''''proxy_used_mb''''),
        (''''source_pilot_runs'''',''''captcha_cost''''),
        (''''source_pilot_runs'''',''''html_sha256''''),
        (''''source_pilot_runs'''',''''screenshot_sha256''''),
        (''''source_pilot_runs'''',''''html_gzip''''),
        (''''source_pilot_runs'''',''''screenshot_png''''),
        (''''source_pilot_runs'''',''''manifest''''),
        (''''source_pilot_predictions'''',''''run_id''''),
        (''''source_pilot_predictions'''',''''source''''),
        (''''source_pilot_predictions'''',''''source_match_key''''),
        (''''source_pilot_predictions'''',''''home_team''''),
        (''''source_pilot_predictions'''',''''away_team''''),
        (''''source_pilot_predictions'''',''''league''''),
        (''''source_pilot_predictions'''',''''kickoff_raw''''),
        (''''source_pilot_predictions'''',''''source_timezone''''),
        (''''source_pilot_predictions'''',''''kickoff_utc''''),
        (''''source_pilot_predictions'''',''''timezone_status''''),
        (''''source_pilot_predictions'''',''''p1''''),
        (''''source_pilot_predictions'''',''''px''''),
        (''''source_pilot_predictions'''',''''p2''''),
        (''''source_pilot_predictions'''',''''over25''''),
        (''''source_pilot_predictions'''',''''under25''''),
        (''''source_pilot_predictions'''',''''btts_yes''''),
        (''''source_pilot_predictions'''',''''btts_no''''),
        (''''source_pilot_predictions'''',''''xg_home''''),
        (''''source_pilot_predictions'''',''''xg_away''''),
        (''''source_pilot_predictions'''',''''correct_score''''),
        (''''source_pilot_predictions'''',''''parse_status''''),
        (''''source_pilot_predictions'''',''''payload''''),
        (''''source_pilot_matches'''',''''run_id''''),
        (''''source_pilot_matches'''',''''source_match_key''''),
        (''''source_pilot_matches'''',''''anchor_match_id''''),
        (''''source_pilot_matches'''',''''match_status''''),
        (''''source_pilot_matches'''',''''match_confidence''''),
        (''''source_pilot_matches'''',''''reason''''),
        (''''source_pilot_control'''',''''pilot_id''''),
        (''''source_pilot_control'''',''''started_at_utc''''),
        (''''source_pilot_control'''',''''ends_at_utc''''),
        (''''source_pilot_control'''',''''status''''),
        (''''source_pilot_control'''',''''slots_expected''''),
        (''''source_pilot_control'''',''''updated_at''''),
        (''''strategy_simulations'''',''''batch_id''''),
        (''''strategy_simulations'''',''''match_id''''),
        (''''strategy_simulations'''',''''strategy_id''''),
        (''''strategy_simulations'''',''''strategy_name''''),
        (''''strategy_simulations'''',''''kickoff_utc''''),
        (''''strategy_simulations'''',''''home_team''''),
        (''''strategy_simulations'''',''''away_team''''),
        (''''strategy_simulations'''',''''pick''''),
        (''''strategy_simulations'''',''''probability''''),
        (''''strategy_simulations'''',''''odds''''),
        (''''strategy_simulations'''',''''edge''''),
        (''''strategy_simulations'''',''''stake_eur''''),
        (''''strategy_simulations'''',''''priced''''),
        (''''strategy_simulations'''',''''status''''),
        (''''strategy_simulations'''',''''result_1x2''''),
        (''''strategy_simulations'''',''''correct''''),
        (''''strategy_simulations'''',''''profit_eur''''),
        (''''strategy_simulations'''',''''source_snapshot_timestamp_utc''''),
        (''''strategy_simulations'''',''''updated_at''''),
        (''''strategy_stats'''',''''strategy_id''''),
        (''''strategy_stats'''',''''strategy_name''''),
        (''''strategy_stats'''',''''description''''),
        (''''strategy_stats'''',''''stake_eur''''),
        (''''strategy_stats'''',''''signals_total''''),
        (''''strategy_stats'''',''''pending''''),
        (''''strategy_stats'''',''''settled''''),
        (''''strategy_stats'''',''''priced_settled''''),
        (''''strategy_stats'''',''''wins''''),
        (''''strategy_stats'''',''''losses''''),
        (''''strategy_stats'''',''''staked_eur''''),
        (''''strategy_stats'''',''''profit_eur''''),
        (''''strategy_stats'''',''''cumulative_profit_eur''''),
        (''''strategy_stats'''',''''win_rate''''),
        (''''strategy_stats'''',''''roi''''),
        (''''strategy_stats'''',''''sample_reliability''''),
        (''''strategy_stats'''',''''updated_at''''),
        (''''market_decision_tickets'''',''''ticket_id''''),
        (''''market_decision_tickets'''',''''batch_id''''),
        (''''market_decision_tickets'''',''''match_id''''),
        (''''market_decision_tickets'''',''''kickoff_utc''''),
        (''''market_decision_tickets'''',''''home_team''''),
        (''''market_decision_tickets'''',''''away_team''''),
        (''''market_decision_tickets'''',''''league''''),
        (''''market_decision_tickets'''',''''source''''),
        (''''market_decision_tickets'''',''''market_id''''),
        (''''market_decision_tickets'''',''''period''''),
        (''''market_decision_tickets'''',''''selection''''),
        (''''market_decision_tickets'''',''''line''''),
        (''''market_decision_tickets'''',''''probability''''),
        (''''market_decision_tickets'''',''''odds''''),
        (''''market_decision_tickets'''',''''edge''''),
        (''''market_decision_tickets'''',''''derivation''''),
        (''''market_decision_tickets'''',''''source_snapshot_utc''''),
        (''''market_decision_tickets'''',''''status''''),
        (''''market_decision_tickets'''',''''result_value''''),
        (''''market_decision_tickets'''',''''correct''''),
        (''''market_decision_tickets'''',''''stake_eur''''),
        (''''market_decision_tickets'''',''''profit_eur''''),
        (''''market_decision_tickets'''',''''payload''''),
        (''''market_decision_tickets'''',''''created_at''''),
        (''''market_decision_tickets'''',''''updated_at''''),
        (''''market_stats'''',''''market_id''''),
        (''''market_stats'''',''''source''''),
        (''''market_stats'''',''''signals_total''''),
        (''''market_stats'''',''''pending''''),
        (''''market_stats'''',''''settled''''),
        (''''market_stats'''',''''priced_settled''''),
        (''''market_stats'''',''''wins''''),
        (''''market_stats'''',''''losses''''),
        (''''market_stats'''',''''staked_eur''''),
        (''''market_stats'''',''''profit_eur''''),
        (''''market_stats'''',''''win_rate''''),
        (''''market_stats'''',''''roi''''),
        (''''market_stats'''',''''sample_reliability''''),
        (''''market_stats'''',''''updated_at''''),
        (''''result_observations'''',''''observation_id''''),
        (''''result_observations'''',''''match_id''''),
        (''''result_observations'''',''''provider''''),
        (''''result_observations'''',''''provider_match_id''''),
        (''''result_observations'''',''''observed_at_utc''''),
        (''''result_observations'''',''''kickoff_utc''''),
        (''''result_observations'''',''''status''''),
        (''''result_observations'''',''''final_home_goals''''),
        (''''result_observations'''',''''final_away_goals''''),
        (''''result_observations'''',''''ht_home_goals''''),
        (''''result_observations'''',''''ht_away_goals''''),
        (''''result_observations'''',''''corners_home''''),
        (''''result_observations'''',''''corners_away''''),
        (''''result_observations'''',''''ht_corners_home''''),
        (''''result_observations'''',''''ht_corners_away''''),
        (''''result_observations'''',''''raw_sha256''''),
        (''''result_observations'''',''''payload''''),
        (''''result_consensus'''',''''match_id''''),
        (''''result_consensus'''',''''final_home_goals''''),
        (''''result_consensus'''',''''final_away_goals''''),
        (''''result_consensus'''',''''ht_home_goals''''),
        (''''result_consensus'''',''''ht_away_goals''''),
        (''''result_consensus'''',''''corners_home''''),
        (''''result_consensus'''',''''corners_away''''),
        (''''result_consensus'''',''''ht_corners_home''''),
        (''''result_consensus'''',''''ht_corners_away''''),
        (''''result_consensus'''',''''result_1x2''''),
        (''''result_consensus'''',''''verification_status''''),
        (''''result_consensus'''',''''field_verification''''),
        (''''result_consensus'''',''''providers''''),
        (''''result_consensus'''',''''updated_at''''),
        (''''result_source_pilot_control'''',''''pilot_id''''),
        (''''result_source_pilot_control'''',''''started_at_utc''''),
        (''''result_source_pilot_control'''',''''ends_at_utc''''),
        (''''result_source_pilot_control'''',''''status''''),
        (''''result_source_pilot_control'''',''''slots_expected''''),
        (''''result_source_pilot_control'''',''''updated_at''''),
        (''''result_source_pilot_runs'''',''''run_id''''),
        (''''result_source_pilot_runs'''',''''pilot_id''''),
        (''''result_source_pilot_runs'''',''''source''''),
        (''''result_source_pilot_runs'''',''''slot_no''''),
        (''''result_source_pilot_runs'''',''''started_at_utc''''),
        (''''result_source_pilot_runs'''',''''finished_at_utc''''),
        (''''result_source_pilot_runs'''',''''status''''),
        (''''result_source_pilot_runs'''',''''final_url''''),
        (''''result_source_pilot_runs'''',''''http_status''''),
        (''''result_source_pilot_runs'''',''''browser_session_id''''),
        (''''result_source_pilot_runs'''',''''browser_cost''''),
        (''''result_source_pilot_runs'''',''''proxy_cost''''),
        (''''result_source_pilot_runs'''',''''proxy_used_mb''''),
        (''''result_source_pilot_runs'''',''''captcha_cost''''),
        (''''result_source_pilot_runs'''',''''rows_found''''),
        (''''result_source_pilot_runs'''',''''capabilities''''),
        (''''result_source_pilot_runs'''',''''html_sha256''''),
        (''''result_source_pilot_runs'''',''''screenshot_sha256''''),
        (''''result_source_pilot_runs'''',''''html_gzip''''),
        (''''result_source_pilot_runs'''',''''screenshot_png''''),
        (''''result_source_pilot_runs'''',''''manifest'''')
        ) AS required(table_name,column_name)
        WHERE NOT EXISTS (
            SELECT 1 FROM information_schema.columns c
            WHERE c.table_schema=current_schema()
              AND c.table_name=required.table_name AND c.column_name=required.column_name
        )
    ) THEN
        RAISE EXCEPTION ''''Existing schema is missing baseline columns; explicit repair migration required'''';
    END IF;
END; $baseline_check$;
'';
            INSERT INTO schema_migrations(version,name,sha256)
            VALUES (1,''0001_baseline.sql'',''a6cd3bda6f1bfb8f029d4039b2c50d0ed8d444bc12470b28a382faac068f7287'');
        END IF;
    END;';
DO '
    DECLARE applied schema_migrations%ROWTYPE;
    BEGIN
        SELECT * INTO applied FROM schema_migrations WHERE version=2;
        IF FOUND THEN
            IF applied.name <> ''0002_decision_requests.sql'' OR applied.sha256 <> ''a4a889aaf87ef74a40fb8453b96cbea451b6673ab5dda3eed2e127d906b21012'' THEN
                RAISE EXCEPTION ''Migration 2 checksum/name drift'';
            END IF;
        ELSE
            EXECUTE ''CREATE TABLE decision_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    batch_id TEXT NOT NULL UNIQUE REFERENCES decision_batches(batch_id) ON DELETE RESTRICT,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
'';
            INSERT INTO schema_migrations(version,name,sha256)
            VALUES (2,''0002_decision_requests.sql'',''a4a889aaf87ef74a40fb8453b96cbea451b6673ab5dda3eed2e127d906b21012'');
        END IF;
    END;';
DO '
    DECLARE applied schema_migrations%ROWTYPE;
    BEGIN
        SELECT * INTO applied FROM schema_migrations WHERE version=3;
        IF FOUND THEN
            IF applied.name <> ''0003_browser_session_leases.sql'' OR applied.sha256 <> ''4cb5e76853a41fad80ca8f5cb11f5415e3f7dec70a7e351548e00b51bd3e172e'' THEN
                RAISE EXCEPTION ''Migration 3 checksum/name drift'';
            END IF;
        ELSE
            EXECUTE ''CREATE TABLE browser_session_leases (
    session_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL CHECK (state IN (''''ACTIVE'''',''''STOPPING'''',''''STOP_FAILED'''',''''STOPPED'''')),
    stop_error TEXT NOT NULL DEFAULT '''''''',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX browser_session_leases_reap
ON browser_session_leases(lease_until,last_activity_at) WHERE state <> ''''STOPPED'''';
'';
            INSERT INTO schema_migrations(version,name,sha256)
            VALUES (3,''0003_browser_session_leases.sql'',''4cb5e76853a41fad80ca8f5cb11f5415e3f7dec70a7e351548e00b51bd3e172e'');
        END IF;
    END;';
DO '
    DECLARE applied schema_migrations%ROWTYPE;
    BEGIN
        SELECT * INTO applied FROM schema_migrations WHERE version=4;
        IF FOUND THEN
            IF applied.name <> ''0004_canonical_entities.sql'' OR applied.sha256 <> ''483b2391d9b4af2823f3d74784562777a16e41c4e11c0d9415aea2cafa715a8f'' THEN
                RAISE EXCEPTION ''Migration 4 checksum/name drift'';
            END IF;
        ELSE
            EXECUTE ''-- Internal identities are separate from source eligibility and frozen snapshots.
CREATE TABLE canonical_entities (
    entity_id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN (''''TEAM'''',''''COMPETITION'''')),
    canonical_name TEXT NOT NULL CHECK (length(trim(canonical_name)) > 0),
    country_code TEXT NOT NULL CHECK (country_code ~ ''''^[A-Z]{2,3}$''''),
    category TEXT NOT NULL CHECK (category ~ ''''^(MEN|WOMEN|MIXED):(SENIOR|U[0-9]{2}):(FIRST|B|II|RESERVE)$''''),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance)=''''object'''' AND provenance <> ''''{}''''::jsonb),
    external_ids JSONB NOT NULL DEFAULT ''''{}'''' CHECK (jsonb_typeof(external_ids)=''''object''''),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (entity_id,kind,country_code,category)
);

CREATE TABLE canonical_alias_versions (
    alias_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    entity_id UUID NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL CHECK (length(trim(source)) > 0 AND source <> ''''*''''),
    alias TEXT NOT NULL CHECK (length(trim(alias)) > 0),
    normalized_alias TEXT NOT NULL CHECK (length(trim(normalized_alias)) > 0),
    country_code TEXT NOT NULL,
    category TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    state TEXT NOT NULL CHECK (state IN (''''ACTIVE'''',''''RETRACTED'''')),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance)=''''object'''' AND provenance <> ''''{}''''::jsonb),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (alias_id,version),
    FOREIGN KEY (entity_id,kind,country_code,category)
        REFERENCES canonical_entities(entity_id,kind,country_code,category),
    CHECK (valid_until IS NULL OR valid_until > valid_from)
);
CREATE INDEX canonical_alias_lookup ON canonical_alias_versions
    (kind,source,normalized_alias,country_code,category,recorded_at);

CREATE FUNCTION canonical_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION ''''Canonical registry is append-only; append an alias revision'''';
END;
$$;
CREATE TRIGGER canonical_entities_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON canonical_entities FOR EACH STATEMENT EXECUTE FUNCTION canonical_reject_mutation();
CREATE TRIGGER canonical_aliases_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON canonical_alias_versions FOR EACH STATEMENT EXECUTE FUNCTION canonical_reject_mutation();

CREATE FUNCTION canonical_alias_revision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE previous canonical_alias_versions%ROWTYPE;
BEGIN
    -- Serialize revisions, including two concurrent first inserts.
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.alias_id,460244124));
    SELECT * INTO previous FROM canonical_alias_versions
        WHERE alias_id=NEW.alias_id ORDER BY version DESC LIMIT 1;
    IF FOUND THEN
        IF NEW.version <> previous.version+1 OR
           ROW(NEW.entity_id,NEW.kind,NEW.source,NEW.alias,NEW.normalized_alias,NEW.country_code,NEW.category)
           IS DISTINCT FROM
           ROW(previous.entity_id,previous.kind,previous.source,previous.alias,previous.normalized_alias,previous.country_code,previous.category)
        THEN RAISE EXCEPTION ''''Alias revision sequence or identity changed''''; END IF;
    ELSIF NEW.version <> 1 THEN
        RAISE EXCEPTION ''''Alias revisions must start at version 1'''';
    END IF;
    -- Knowledge time is server-owned; it cannot be backdated by an import.
    NEW.recorded_at := clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER canonical_alias_version_insert BEFORE INSERT ON canonical_alias_versions
    FOR EACH ROW EXECUTE FUNCTION canonical_alias_revision_guard();
'';
            INSERT INTO schema_migrations(version,name,sha256)
            VALUES (4,''0004_canonical_entities.sql'',''483b2391d9b4af2823f3d74784562777a16e41c4e11c0d9415aea2cafa715a8f'');
        END IF;
    END;';
DO '
    DECLARE applied schema_migrations%ROWTYPE;
    BEGIN
        SELECT * INTO applied FROM schema_migrations WHERE version=5;
        IF FOUND THEN
            IF applied.name <> ''0005_pilot_server_sealing.sql'' OR applied.sha256 <> ''ee7ad29edc404f38608404216fd55da795eba597fcd6d6eddb7e7f80001fbd56'' THEN
                RAISE EXCEPTION ''Migration 5 checksum/name drift'';
            END IF;
        ELSE
            EXECUTE ''-- Existing rows deliberately remain NULL: no retroactive certification.
ALTER TABLE source_pilot_runs ADD COLUMN sealed_at_utc TIMESTAMPTZ;
ALTER TABLE source_pilot_predictions ADD COLUMN sealed_at_utc TIMESTAMPTZ;

CREATE FUNCTION seal_pilot_row() RETURNS trigger LANGUAGE plpgsql AS $seal$
BEGIN
    IF TG_OP <> ''''INSERT'''' THEN
        RAISE EXCEPTION ''''Pilot evidence is append-only; do not rewrite historical slots'''';
    END IF;
    IF NEW.sealed_at_utc IS NOT NULL THEN
        RAISE EXCEPTION ''''Pilot sealing time is server-owned'''';
    END IF;
    NEW.sealed_at_utc := clock_timestamp();
    RETURN NEW;
END;
$seal$;

CREATE TRIGGER source_pilot_runs_seal
    BEFORE INSERT OR UPDATE OR DELETE ON source_pilot_runs
    FOR EACH ROW EXECUTE FUNCTION seal_pilot_row();
CREATE TRIGGER source_pilot_predictions_seal
    BEFORE INSERT OR UPDATE OR DELETE ON source_pilot_predictions
    FOR EACH ROW EXECUTE FUNCTION seal_pilot_row();
CREATE TRIGGER source_pilot_runs_no_truncate
    BEFORE TRUNCATE ON source_pilot_runs
    FOR EACH STATEMENT EXECUTE FUNCTION seal_pilot_row();
CREATE TRIGGER source_pilot_predictions_no_truncate
    BEFORE TRUNCATE ON source_pilot_predictions
    FOR EACH STATEMENT EXECUTE FUNCTION seal_pilot_row();
'';
            INSERT INTO schema_migrations(version,name,sha256)
            VALUES (5,''0005_pilot_server_sealing.sql'',''ee7ad29edc404f38608404216fd55da795eba597fcd6d6eddb7e7f80001fbd56'');
        END IF;
    END;';
DO '
    DECLARE applied schema_migrations%ROWTYPE;
    BEGIN
        SELECT * INTO applied FROM schema_migrations WHERE version=6;
        IF FOUND THEN
            IF applied.name <> ''0006_evaluation_protocols.sql'' OR applied.sha256 <> ''08253596be53ccc1273a7f956793b6c20ae28333c5dc7553c67d8d530acb13fb'' THEN
                RAISE EXCEPTION ''Migration 6 checksum/name drift'';
            END IF;
        ELSE
            EXECUTE ''-- New evidence only. No historical tickets/snapshots/results are modified.
CREATE TABLE evaluation_protocols (
    protocol_id TEXT PRIMARY KEY CHECK (length(trim(protocol_id)) > 0),
    contract_version TEXT NOT NULL DEFAULT ''''frozen-ticket-v1'''' CHECK (contract_version=''''frozen-ticket-v1''''),
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL CHECK (window_end > window_start),
    policy_json TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    sealed_at_utc TIMESTAMPTZ NOT NULL
);

CREATE FUNCTION seal_evaluation_protocol() RETURNS trigger LANGUAGE plpgsql AS $seal$
DECLARE
    p JSONB;
    scoring JSONB;
    field TEXT;
BEGIN
    IF TG_OP <> ''''INSERT'''' THEN
        RAISE EXCEPTION ''''Evaluation protocols are append-only'''';
    END IF;
    IF NEW.sealed_at_utc IS NOT NULL OR NEW.policy_sha256 IS NOT NULL THEN
        RAISE EXCEPTION ''''Evaluation seal and hash are server-owned'''';
    END IF;
    NEW.sealed_at_utc := clock_timestamp();
    IF NEW.window_start <= NEW.sealed_at_utc THEN
        RAISE EXCEPTION ''''Evaluation must be registered before its future window'''';
    END IF;
    p := NEW.policy_json::jsonb;
    IF jsonb_typeof(p) <> ''''object'''' OR NOT p ?& ARRAY[''''window_start'''',''''window_end'''',''''strategy'''',
        ''''min_fixtures'''',''''min_days'''',''''min_coverage'''',''''min_log_loss_gain'''',''''min_roi'''',
        ''''result_grace_days'''',''''commission'''',''''score_policy''''] OR
        (SELECT count(*) FROM jsonb_object_keys(p)) <> 11 THEN
        RAISE EXCEPTION ''''Complete evaluation policy required'''';
    END IF;
    IF jsonb_typeof(p->''''window_start'''') <> ''''string'''' OR jsonb_typeof(p->''''window_end'''') <> ''''string'''' OR
        jsonb_typeof(p->''''strategy'''') <> ''''string'''' OR
        (p->>''''window_start'''') !~ ''''(Z|[+-][0-9]{2}:[0-9]{2})$'''' OR
        (p->>''''window_end'''') !~ ''''(Z|[+-][0-9]{2}:[0-9]{2})$'''' OR
        (p->>''''window_start'''')::timestamptz <> NEW.window_start OR
        (p->>''''window_end'''')::timestamptz <> NEW.window_end OR
        p->>''''strategy'''' !~ ''''^S[1-9]$'''' THEN
        RAISE EXCEPTION ''''Evaluation window or strategy mismatch'''';
    END IF;
    FOREACH field IN ARRAY ARRAY[''''min_fixtures'''',''''min_days'''',''''min_coverage'''',''''min_log_loss_gain'''',
                                 ''''min_roi'''',''''result_grace_days'''',''''commission''''] LOOP
        IF jsonb_typeof(p->field) <> ''''number'''' THEN
            RAISE EXCEPTION ''''Numeric evaluation threshold required: %'''',field;
        END IF;
    END LOOP;
    FOREACH field IN ARRAY ARRAY[''''min_fixtures'''',''''min_days'''',''''result_grace_days''''] LOOP
        IF (p->>field)::numeric <> trunc((p->>field)::numeric) OR (p->>field)::numeric > 1000000 THEN
            RAISE EXCEPTION ''''Integer evaluation count required: %'''',field;
        END IF;
    END LOOP;
    IF (p->>''''min_fixtures'''')::numeric < 2 OR (p->>''''min_days'''')::numeric < 2 OR
        (p->>''''min_days'''')::numeric > (p->>''''min_fixtures'''')::numeric OR
        (p->>''''result_grace_days'''')::numeric < 0 OR
        (p->>''''min_coverage'''')::numeric <= 0 OR (p->>''''min_coverage'''')::numeric > 1 OR
        (p->>''''min_log_loss_gain'''')::numeric <= 0 OR
        (p->>''''min_roi'''')::numeric <= 0 OR (p->>''''min_roi'''')::numeric >= 1 OR
        (p->>''''commission'''')::numeric < 0 OR (p->>''''commission'''')::numeric >= 1 THEN
        RAISE EXCEPTION ''''Invalid evaluation thresholds'''';
    END IF;
    scoring := p->''''score_policy'''';
    IF jsonb_typeof(scoring) <> ''''object'''' OR NOT scoring ?& ARRAY[''''epsilon'''',''''bin_edges'''',''''min_bin_count'''',
        ''''bootstrap_replicates'''',''''bootstrap_seed'''',''''confidence_level''''] OR
        (SELECT count(*) FROM jsonb_object_keys(scoring)) <> 6 THEN
        RAISE EXCEPTION ''''Complete scoring policy required'''';
    END IF;
    FOREACH field IN ARRAY ARRAY[''''epsilon'''',''''min_bin_count'''',''''bootstrap_replicates'''',
                                 ''''bootstrap_seed'''',''''confidence_level''''] LOOP
        IF jsonb_typeof(scoring->field) <> ''''number'''' THEN
            RAISE EXCEPTION ''''Numeric scoring parameter required: %'''',field;
        END IF;
    END LOOP;
    FOREACH field IN ARRAY ARRAY[''''min_bin_count'''',''''bootstrap_replicates'''',''''bootstrap_seed''''] LOOP
        IF (scoring->>field)::numeric <> trunc((scoring->>field)::numeric) THEN
            RAISE EXCEPTION ''''Integer scoring parameter required: %'''',field;
        END IF;
    END LOOP;
    IF (scoring->>''''epsilon'''')::numeric <= 0 OR (scoring->>''''epsilon'''')::numeric >= 0.5 OR
        (scoring->>''''min_bin_count'''')::numeric < 1 OR (scoring->>''''min_bin_count'''')::numeric > 1000000 OR
        (scoring->>''''bootstrap_replicates'''')::numeric < 1000 OR (scoring->>''''bootstrap_replicates'''')::numeric > 10000 OR
        (scoring->>''''confidence_level'''')::numeric < 0.95 OR (scoring->>''''confidence_level'''')::numeric >= 1 OR
        jsonb_typeof(scoring->''''bin_edges'''') <> ''''array'''' THEN
        RAISE EXCEPTION ''''Invalid scoring or uncertainty policy'''';
    END IF;
    IF jsonb_array_length(scoring->''''bin_edges'''') < 2 OR
        scoring->''''bin_edges''''->0 <> ''''0''''::jsonb OR scoring->''''bin_edges''''->-1 <> ''''1''''::jsonb OR
        EXISTS (SELECT 1 FROM jsonb_array_elements(scoring->''''bin_edges'''') WITH ORDINALITY AS edges(value,pos)
            WHERE jsonb_typeof(value) <> ''''number'''' OR
                (pos>1 AND value::numeric <= (scoring->''''bin_edges''''->(pos::int-2))::numeric)) THEN
        RAISE EXCEPTION ''''Invalid fixed calibration bins'''';
    END IF;
    NEW.policy_sha256 := encode(sha256(convert_to(NEW.policy_json,''''UTF8'''')),''''hex'''');
    RETURN NEW;
END;
$seal$;

CREATE TRIGGER evaluation_protocols_seal BEFORE INSERT OR UPDATE OR DELETE ON evaluation_protocols
    FOR EACH ROW EXECUTE FUNCTION seal_evaluation_protocol();
CREATE TRIGGER evaluation_protocols_no_truncate BEFORE TRUNCATE ON evaluation_protocols
    FOR EACH STATEMENT EXECUTE FUNCTION seal_evaluation_protocol();
'';
            INSERT INTO schema_migrations(version,name,sha256)
            VALUES (6,''0006_evaluation_protocols.sql'',''08253596be53ccc1273a7f956793b6c20ae28333c5dc7553c67d8d530acb13fb'');
        END IF;
    END;';
DO '
    DECLARE applied schema_migrations%ROWTYPE;
    BEGIN
        SELECT * INTO applied FROM schema_migrations WHERE version=7;
        IF FOUND THEN
            IF applied.name <> ''0007_sxbet_rate_limits.sql'' OR applied.sha256 <> ''3f3f06d34cf7e83e01a096bd7366eeeecac2edd5d05b1039c491ea64183b74e4'' THEN
                RAISE EXCEPTION ''Migration 7 checksum/name drift'';
            END IF;
        ELSE
            EXECUTE ''-- SX Bet shared rate coordinator (governance §2.4). Additive only.
-- Source of truth for bridge/sxbet/ratelimit.py:_SCHEMA, which repeats these
-- statements verbatim so a process can ensure them idempotently (IF NOT EXISTS
-- makes either order safe). Bucket keys hold a SHA-256 of the API key, never the key.
CREATE TABLE IF NOT EXISTS sxbet_rate_attempts (
    attempt_id TEXT PRIMARY KEY,
    bucket_key TEXT NOT NULL CHECK (bucket_key LIKE ''''sxbet:v1:%'''' AND length(bucket_key) <= 256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_sxbet_rate_attempts_bucket
ON sxbet_rate_attempts(bucket_key, created_at);
CREATE INDEX IF NOT EXISTS idx_sxbet_rate_attempts_created
ON sxbet_rate_attempts(created_at);
CREATE TABLE IF NOT EXISTS sxbet_rate_cooldowns (
    bucket_key TEXT PRIMARY KEY CHECK (bucket_key LIKE ''''sxbet:v1:%'''' AND length(bucket_key) <= 256),
    cooldown_until TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
'';
            INSERT INTO schema_migrations(version,name,sha256)
            VALUES (7,''0007_sxbet_rate_limits.sql'',''3f3f06d34cf7e83e01a096bd7366eeeecac2edd5d05b1039c491ea64183b74e4'');
        END IF;
    END;';
