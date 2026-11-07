from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREPARE = (ROOT / "sql/aiven/prepare_bridge_roles.sql").read_text()
FINALIZE = (ROOT / "sql/aiven/finalize_bridge_privileges.sql").read_text()


def test_role_scripts_never_create_or_embed_credentials():
    combined = (PREPARE + FINALIZE).lower()
    assert "create role" not in combined
    assert "password" not in combined
    assert "signer_schema_migrations" not in combined
    assert "signer_ledger" not in combined


def test_app_has_explicit_runtime_allowlist_and_no_schema_create():
    assert "GRANT USAGE ON SCHEMA public TO fcb_app" in PREPARE
    assert "GRANT USAGE, CREATE ON SCHEMA public TO fcb_app" not in PREPARE
    for table in (
        "oauth_authorization_codes", "oauth_access_tokens", "oauth_refresh_tokens",
        "oauth_claude_dcr_clients", "browser_session_leases",
        "api_football_quota_counters", "api_football_quota_leases",
        "api_football_quota_attempts", "sxbet_rate_attempts", "sxbet_rate_cooldowns",
    ):
        assert table in FINALIZE


def test_signer_is_explicitly_revoked_and_never_granted():
    assert "REVOKE ALL ON SCHEMA public FROM fcb_signer" in PREPARE
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, fcb_app, fcb_signer" in FINALIZE
    assert "TO fcb_signer" not in FINALIZE
