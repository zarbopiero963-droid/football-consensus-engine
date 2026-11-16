-- SX Bet isolated signer: published status read by the MCP bridge before queueing a trading request (#187, P5).
-- DRAFT, NOT in the signer migration series yet (same promotion step as 0002, coordinated with WP-009 and the
-- owner). Signer database only, never the bridge database. Additive.
-- STATUS, not audit: one row per signer_id, overwritten in place by the signer after every worker cycle. The
-- audit stays in the append-only ledger and signer_order_request_events. Roles (owner step, WP-009): signer
-- INSERT/UPDATE/SELECT; MCP SELECT only. No DELETE/TRUNCATE for either.
CREATE TABLE IF NOT EXISTS signer_status (
    signer_id TEXT PRIMARY KEY CHECK (length(signer_id) BETWEEN 1 AND 200),
    network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
    kill_switch TEXT NOT NULL CHECK (kill_switch IN ('ENGAGED', 'DISENGAGED', 'UNKNOWN')),
    live_trading TEXT NOT NULL CHECK (live_trading IN ('AUTHORIZED', 'NOT_AUTHORIZED')),
    live_reason TEXT CHECK (live_reason IS NULL OR live_reason ~ '^[A-Z0-9_]{1,64}$'),
    last_cycle_ok BOOLEAN NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK ((live_trading = 'AUTHORIZED') = (live_reason IS NULL))
);
