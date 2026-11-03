-- FCB-WP-005: read-only precheck for a schema that already has schema_migrations.
-- Prints exactly one word: BRIDGE_0001_0007 (upgrade to 0008) or BRIDGE_0001_0008 (no-op) may
-- proceed; every REFUSE_* state stops the restore. Names AND checksums must match exactly.
-- The rows below are pinned to sql/migrations/bridge (tests keep them equal).
WITH expected(version, name, sha256) AS (VALUES
    (1, '0001_baseline.sql', 'a6cd3bda6f1bfb8f029d4039b2c50d0ed8d444bc12470b28a382faac068f7287'),
    (2, '0002_decision_requests.sql', 'a4a889aaf87ef74a40fb8453b96cbea451b6673ab5dda3eed2e127d906b21012'),
    (3, '0003_browser_session_leases.sql', '4cb5e76853a41fad80ca8f5cb11f5415e3f7dec70a7e351548e00b51bd3e172e'),
    (4, '0004_canonical_entities.sql', '483b2391d9b4af2823f3d74784562777a16e41c4e11c0d9415aea2cafa715a8f'),
    (5, '0005_pilot_server_sealing.sql', 'ee7ad29edc404f38608404216fd55da795eba597fcd6d6eddb7e7f80001fbd56'),
    (6, '0006_evaluation_protocols.sql', '08253596be53ccc1273a7f956793b6c20ae28333c5dc7553c67d8d530acb13fb'),
    (7, '0007_sxbet_rate_limits.sql', '3f3f06d34cf7e83e01a096bd7366eeeecac2edd5d05b1039c491ea64183b74e4'),
    (8, '0008_runtime_tables.sql', '2ee5a7bbe9ce1a8b55474ba73288c1dfda6c847f81b7277bf5956f080dab759d')
),
got AS (SELECT version, name, sha256 FROM schema_migrations),
n AS (SELECT count(*) AS rows, count(DISTINCT version) AS versions FROM got)
SELECT CASE
    WHEN EXISTS (SELECT 1 FROM got WHERE version = 8 AND name = '0008_signer_ledger_anchor.sql')
        THEN 'REFUSE_PRE_SPLIT_SIGNER_0008'
    WHEN (SELECT rows FROM n) = 7 AND (SELECT versions FROM n) = 7 AND NOT EXISTS (
            SELECT version, name, sha256 FROM got
            EXCEPT SELECT version, name, sha256 FROM expected WHERE version <= 7)
        THEN 'BRIDGE_0001_0007'
    WHEN (SELECT rows FROM n) = 8 AND (SELECT versions FROM n) = 8 AND NOT EXISTS (
            SELECT version, name, sha256 FROM got EXCEPT SELECT version, name, sha256 FROM expected)
        THEN 'BRIDGE_0001_0008'
    ELSE 'REFUSE_PARTIAL_OR_DRIFT'
END;
