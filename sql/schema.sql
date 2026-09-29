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
