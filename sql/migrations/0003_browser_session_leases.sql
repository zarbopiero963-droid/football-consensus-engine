CREATE TABLE browser_session_leases (
    session_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE','STOPPING','STOP_FAILED','STOPPED')),
    stop_error TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX browser_session_leases_reap
ON browser_session_leases(lease_until,last_activity_at) WHERE state <> 'STOPPED';
