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


CREATE TABLE IF NOT EXISTS market_decision_tickets (
    ticket_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES decision_batches(batch_id) ON DELETE RESTRICT,
    match_id TEXT NOT NULL,
    kickoff_utc TIMESTAMPTZ NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    league TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    market_id TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT 'FT',
    selection TEXT NOT NULL,
    line DOUBLE PRECISION,
    probability DOUBLE PRECISION,
    odds DOUBLE PRECISION,
    edge DOUBLE PRECISION,
    derivation TEXT NOT NULL DEFAULT 'SOURCE',
    source_snapshot_utc TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    result_value TEXT,
    correct BOOLEAN,
    stake_eur NUMERIC(12,2) NOT NULL DEFAULT 1.00,
    profit_eur NUMERIC(14,4),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
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
    sample_reliability TEXT NOT NULL DEFAULT 'INSUFFICIENT_SAMPLE',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(market_id,source)
);


CREATE TABLE IF NOT EXISTS result_observations (
    observation_id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_match_id TEXT NOT NULL DEFAULT '',
    observed_at_utc TIMESTAMPTZ NOT NULL,
    kickoff_utc TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT '',
    final_home_goals INTEGER,
    final_away_goals INTEGER,
    ht_home_goals INTEGER,
    ht_away_goals INTEGER,
    corners_home INTEGER,
    corners_away INTEGER,
    ht_corners_home INTEGER,
    ht_corners_away INTEGER,
    raw_sha256 TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
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
    verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
    field_verification JSONB NOT NULL DEFAULT '{}'::jsonb,
    providers JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS result_source_pilot_control (
    pilot_id TEXT PRIMARY KEY,
    started_at_utc TIMESTAMPTZ NOT NULL,
    ends_at_utc TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
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
    final_url TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    browser_session_id TEXT NOT NULL DEFAULT '',
    browser_cost DOUBLE PRECISION,
    proxy_cost DOUBLE PRECISION,
    proxy_used_mb DOUBLE PRECISION,
    captcha_cost DOUBLE PRECISION,
    rows_found INTEGER NOT NULL DEFAULT 0,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    html_sha256 TEXT NOT NULL DEFAULT '',
    screenshot_sha256 TEXT NOT NULL DEFAULT '',
    html_gzip BYTEA,
    screenshot_png BYTEA,
    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (pilot_id, source, slot_no)
);

CREATE INDEX IF NOT EXISTS idx_result_source_pilot_runs
ON result_source_pilot_runs (pilot_id, source, slot_no);

-- Refuse to certify a pre-existing schema that lacks required baseline columns.
DO $baseline_check$ BEGIN
    IF EXISTS (
        SELECT 1 FROM (VALUES
        ('model_snapshots','match_id'),
        ('model_snapshots','snapshot_timestamp_utc'),
        ('model_snapshots','kickoff_utc'),
        ('model_snapshots','home_team'),
        ('model_snapshots','away_team'),
        ('model_snapshots','league'),
        ('model_snapshots','source_match_id'),
        ('model_snapshots','valid_prematch'),
        ('model_snapshots','analysis_eligible'),
        ('model_snapshots','model_pick'),
        ('model_snapshots','model_pick_probability'),
        ('model_snapshots','picked_odds'),
        ('model_snapshots','picked_edge'),
        ('model_snapshots','pin_favorite'),
        ('model_snapshots','pin_favorite_odds'),
        ('model_snapshots','same_as_favorite'),
        ('model_snapshots','sha256_model_raw'),
        ('model_snapshots','sha256_odds_raw'),
        ('model_snapshots','payload'),
        ('model_snapshots','imported_at'),
        ('match_results','match_id'),
        ('match_results','version'),
        ('match_results','status'),
        ('match_results','result_1x2'),
        ('match_results','final_home_goals'),
        ('match_results','final_away_goals'),
        ('match_results','result_timestamp_utc'),
        ('match_results','result_source'),
        ('match_results','payload'),
        ('match_results','imported_at'),
        ('ingest_runs','id'),
        ('ingest_runs','source'),
        ('ingest_runs','source_sha256'),
        ('ingest_runs','rows_seen'),
        ('ingest_runs','rows_inserted'),
        ('ingest_runs','created_at'),
        ('runtime_state','key'),
        ('runtime_state','value'),
        ('runtime_state','updated_at'),
        ('oauth_authorization_codes','code_hash'),
        ('oauth_authorization_codes','client_id'),
        ('oauth_authorization_codes','redirect_uri'),
        ('oauth_authorization_codes','code_challenge'),
        ('oauth_authorization_codes','resource'),
        ('oauth_authorization_codes','scope'),
        ('oauth_authorization_codes','expires_at'),
        ('oauth_authorization_codes','used_at'),
        ('oauth_access_tokens','token_hash'),
        ('oauth_access_tokens','client_id'),
        ('oauth_access_tokens','resource'),
        ('oauth_access_tokens','scope'),
        ('oauth_access_tokens','expires_at'),
        ('oauth_access_tokens','revoked_at'),
        ('oauth_access_tokens','created_at'),
        ('oauth_refresh_tokens','token_hash'),
        ('oauth_refresh_tokens','client_id'),
        ('oauth_refresh_tokens','resource'),
        ('oauth_refresh_tokens','scope'),
        ('oauth_refresh_tokens','expires_at'),
        ('oauth_refresh_tokens','revoked_at'),
        ('oauth_refresh_tokens','created_at'),
        ('decision_batches','batch_id'),
        ('decision_batches','requested_at_utc'),
        ('decision_batches','requested_date'),
        ('decision_batches','timezone'),
        ('decision_batches','mode'),
        ('decision_batches','strategy_filter'),
        ('decision_batches','label'),
        ('decision_batches','created_at'),
        ('decision_tickets','batch_id'),
        ('decision_tickets','match_id'),
        ('decision_tickets','snapshot_timestamp_utc'),
        ('decision_tickets','kickoff_utc'),
        ('decision_tickets','home_team'),
        ('decision_tickets','away_team'),
        ('decision_tickets','league'),
        ('decision_tickets','model_pick'),
        ('decision_tickets','model_probability'),
        ('decision_tickets','picked_odds'),
        ('decision_tickets','picked_edge'),
        ('decision_tickets','strategies'),
        ('decision_tickets','display_status'),
        ('decision_tickets','snapshot_view'),
        ('decision_tickets','shown_to_user'),
        ('decision_tickets','created_at'),
        ('source_pilot_runs','run_id'),
        ('source_pilot_runs','pilot_id'),
        ('source_pilot_runs','source'),
        ('source_pilot_runs','slot_no'),
        ('source_pilot_runs','started_at_utc'),
        ('source_pilot_runs','finished_at_utc'),
        ('source_pilot_runs','status'),
        ('source_pilot_runs','final_url'),
        ('source_pilot_runs','http_status'),
        ('source_pilot_runs','browser_session_id'),
        ('source_pilot_runs','browser_cost'),
        ('source_pilot_runs','proxy_cost'),
        ('source_pilot_runs','proxy_used_mb'),
        ('source_pilot_runs','captcha_cost'),
        ('source_pilot_runs','html_sha256'),
        ('source_pilot_runs','screenshot_sha256'),
        ('source_pilot_runs','html_gzip'),
        ('source_pilot_runs','screenshot_png'),
        ('source_pilot_runs','manifest'),
        ('source_pilot_predictions','run_id'),
        ('source_pilot_predictions','source'),
        ('source_pilot_predictions','source_match_key'),
        ('source_pilot_predictions','home_team'),
        ('source_pilot_predictions','away_team'),
        ('source_pilot_predictions','league'),
        ('source_pilot_predictions','kickoff_raw'),
        ('source_pilot_predictions','source_timezone'),
        ('source_pilot_predictions','kickoff_utc'),
        ('source_pilot_predictions','timezone_status'),
        ('source_pilot_predictions','p1'),
        ('source_pilot_predictions','px'),
        ('source_pilot_predictions','p2'),
        ('source_pilot_predictions','over25'),
        ('source_pilot_predictions','under25'),
        ('source_pilot_predictions','btts_yes'),
        ('source_pilot_predictions','btts_no'),
        ('source_pilot_predictions','xg_home'),
        ('source_pilot_predictions','xg_away'),
        ('source_pilot_predictions','correct_score'),
        ('source_pilot_predictions','parse_status'),
        ('source_pilot_predictions','payload'),
        ('source_pilot_matches','run_id'),
        ('source_pilot_matches','source_match_key'),
        ('source_pilot_matches','anchor_match_id'),
        ('source_pilot_matches','match_status'),
        ('source_pilot_matches','match_confidence'),
        ('source_pilot_matches','reason'),
        ('source_pilot_control','pilot_id'),
        ('source_pilot_control','started_at_utc'),
        ('source_pilot_control','ends_at_utc'),
        ('source_pilot_control','status'),
        ('source_pilot_control','slots_expected'),
        ('source_pilot_control','updated_at'),
        ('strategy_simulations','batch_id'),
        ('strategy_simulations','match_id'),
        ('strategy_simulations','strategy_id'),
        ('strategy_simulations','strategy_name'),
        ('strategy_simulations','kickoff_utc'),
        ('strategy_simulations','home_team'),
        ('strategy_simulations','away_team'),
        ('strategy_simulations','pick'),
        ('strategy_simulations','probability'),
        ('strategy_simulations','odds'),
        ('strategy_simulations','edge'),
        ('strategy_simulations','stake_eur'),
        ('strategy_simulations','priced'),
        ('strategy_simulations','status'),
        ('strategy_simulations','result_1x2'),
        ('strategy_simulations','correct'),
        ('strategy_simulations','profit_eur'),
        ('strategy_simulations','source_snapshot_timestamp_utc'),
        ('strategy_simulations','updated_at'),
        ('strategy_stats','strategy_id'),
        ('strategy_stats','strategy_name'),
        ('strategy_stats','description'),
        ('strategy_stats','stake_eur'),
        ('strategy_stats','signals_total'),
        ('strategy_stats','pending'),
        ('strategy_stats','settled'),
        ('strategy_stats','priced_settled'),
        ('strategy_stats','wins'),
        ('strategy_stats','losses'),
        ('strategy_stats','staked_eur'),
        ('strategy_stats','profit_eur'),
        ('strategy_stats','cumulative_profit_eur'),
        ('strategy_stats','win_rate'),
        ('strategy_stats','roi'),
        ('strategy_stats','sample_reliability'),
        ('strategy_stats','updated_at'),
        ('market_decision_tickets','ticket_id'),
        ('market_decision_tickets','batch_id'),
        ('market_decision_tickets','match_id'),
        ('market_decision_tickets','kickoff_utc'),
        ('market_decision_tickets','home_team'),
        ('market_decision_tickets','away_team'),
        ('market_decision_tickets','league'),
        ('market_decision_tickets','source'),
        ('market_decision_tickets','market_id'),
        ('market_decision_tickets','period'),
        ('market_decision_tickets','selection'),
        ('market_decision_tickets','line'),
        ('market_decision_tickets','probability'),
        ('market_decision_tickets','odds'),
        ('market_decision_tickets','edge'),
        ('market_decision_tickets','derivation'),
        ('market_decision_tickets','source_snapshot_utc'),
        ('market_decision_tickets','status'),
        ('market_decision_tickets','result_value'),
        ('market_decision_tickets','correct'),
        ('market_decision_tickets','stake_eur'),
        ('market_decision_tickets','profit_eur'),
        ('market_decision_tickets','payload'),
        ('market_decision_tickets','created_at'),
        ('market_decision_tickets','updated_at'),
        ('market_stats','market_id'),
        ('market_stats','source'),
        ('market_stats','signals_total'),
        ('market_stats','pending'),
        ('market_stats','settled'),
        ('market_stats','priced_settled'),
        ('market_stats','wins'),
        ('market_stats','losses'),
        ('market_stats','staked_eur'),
        ('market_stats','profit_eur'),
        ('market_stats','win_rate'),
        ('market_stats','roi'),
        ('market_stats','sample_reliability'),
        ('market_stats','updated_at'),
        ('result_observations','observation_id'),
        ('result_observations','match_id'),
        ('result_observations','provider'),
        ('result_observations','provider_match_id'),
        ('result_observations','observed_at_utc'),
        ('result_observations','kickoff_utc'),
        ('result_observations','status'),
        ('result_observations','final_home_goals'),
        ('result_observations','final_away_goals'),
        ('result_observations','ht_home_goals'),
        ('result_observations','ht_away_goals'),
        ('result_observations','corners_home'),
        ('result_observations','corners_away'),
        ('result_observations','ht_corners_home'),
        ('result_observations','ht_corners_away'),
        ('result_observations','raw_sha256'),
        ('result_observations','payload'),
        ('result_consensus','match_id'),
        ('result_consensus','final_home_goals'),
        ('result_consensus','final_away_goals'),
        ('result_consensus','ht_home_goals'),
        ('result_consensus','ht_away_goals'),
        ('result_consensus','corners_home'),
        ('result_consensus','corners_away'),
        ('result_consensus','ht_corners_home'),
        ('result_consensus','ht_corners_away'),
        ('result_consensus','result_1x2'),
        ('result_consensus','verification_status'),
        ('result_consensus','field_verification'),
        ('result_consensus','providers'),
        ('result_consensus','updated_at'),
        ('result_source_pilot_control','pilot_id'),
        ('result_source_pilot_control','started_at_utc'),
        ('result_source_pilot_control','ends_at_utc'),
        ('result_source_pilot_control','status'),
        ('result_source_pilot_control','slots_expected'),
        ('result_source_pilot_control','updated_at'),
        ('result_source_pilot_runs','run_id'),
        ('result_source_pilot_runs','pilot_id'),
        ('result_source_pilot_runs','source'),
        ('result_source_pilot_runs','slot_no'),
        ('result_source_pilot_runs','started_at_utc'),
        ('result_source_pilot_runs','finished_at_utc'),
        ('result_source_pilot_runs','status'),
        ('result_source_pilot_runs','final_url'),
        ('result_source_pilot_runs','http_status'),
        ('result_source_pilot_runs','browser_session_id'),
        ('result_source_pilot_runs','browser_cost'),
        ('result_source_pilot_runs','proxy_cost'),
        ('result_source_pilot_runs','proxy_used_mb'),
        ('result_source_pilot_runs','captcha_cost'),
        ('result_source_pilot_runs','rows_found'),
        ('result_source_pilot_runs','capabilities'),
        ('result_source_pilot_runs','html_sha256'),
        ('result_source_pilot_runs','screenshot_sha256'),
        ('result_source_pilot_runs','html_gzip'),
        ('result_source_pilot_runs','screenshot_png'),
        ('result_source_pilot_runs','manifest')
        ) AS required(table_name,column_name)
        WHERE NOT EXISTS (
            SELECT 1 FROM information_schema.columns c
            WHERE c.table_schema=current_schema()
              AND c.table_name=required.table_name AND c.column_name=required.column_name
        )
    ) THEN
        RAISE EXCEPTION 'Existing schema is missing baseline columns; explicit repair migration required';
    END IF;
END; $baseline_check$;
