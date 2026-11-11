"""AF-07: bridge migration 0010 (API-FOOTBALL certification campaign ledger) and its owner-gated
apply workflow. Offline: file contents, pins and gates only (the database side is
tests/integration/test_apifootball_cert_ledger_postgres.py)."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

from app import migrations

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-apifootball-cert-ledger-0010.yml"
SCRIPT = ROOT / "sql" / "aiven" / "aiven_migrations_bridge_0001_0010.sql"
GRANT = ROOT / "sql" / "aiven" / "apifootball_cert_ledger_0010_grant.sql"
REQUIRE_0009 = ROOT / "sql" / "aiven" / "apifootball_cert_ledger_0010_require_0009.sql"
WP009_FILES = (ROOT / "sql" / "aiven" / "grant_bridge_runtime.sql", ROOT / "sql" / "aiven" / "finalize_bridge_privileges.sql")
LEDGER = ("api_football_cert_campaigns", "api_football_cert_campaign_attempts", "api_football_cert_campaign_outcomes")
SHA256_0009 = "2be8a8b2961b0516c9aba11cbddbcf8001e337de3d3bb46abf7cb932c7f5a56f"
SHA256_0008 = "2ee5a7bbe9ce1a8b55474ba73288c1dfda6c847f81b7277bf5956f080dab759d"
PHRASE = "APPLY BRIDGE 0010 API-FOOTBALL CERT LEDGER TO defaultdb"


def _code(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))


def test_0010_is_the_next_number_after_the_reserved_0009_and_keeps_earlier_bytes():
    items = migrations.load_migrations()
    assert [m.version for m in items] == list(range(1, 11))
    assert items[7].checksum == SHA256_0008 and items[8].checksum == SHA256_0009
    assert items[8].name == "0009_oauth_throttle_buckets.sql"
    assert items[9].name == "0010_apifootball_cert_campaign_ledger.sql"


def test_0010_is_additive_dedicated_and_append_only():
    sql = _code(migrations.load_migrations()[9].sql)
    upper = sql.upper()
    # Dedicated tables: the shared quota coordinator is neither reused nor altered.
    assert "API_FOOTBALL_QUOTA" not in upper
    for word in ("DROP ", "ALTER ", "GRANT ", "REVOKE ", "CREATE ROLE", "SIGNER", "INSERT INTO", "UPDATE ", "DELETE FROM"):
        if word == "UPDATE ":
            assert all("BEFORE UPDATE OR DELETE OR TRUNCATE" in line.upper() for line in sql.splitlines()
                       if "UPDATE " in line.upper()), word
            continue
        assert word not in upper, word
    statements = [s.strip() for s in re.split(r";\s*\n(?=CREATE|$)", sql) if s.strip()]
    assert all(s.upper().startswith(("CREATE TABLE IF NOT EXISTS", "CREATE FUNCTION", "CREATE TRIGGER"))
               for s in statements), statements
    assert "CHECK (BUDGET BETWEEN 1 AND 78)" in upper and "CHECK (SEQ BETWEEN 1 AND 78)" in upper
    assert "PRIMARY KEY (CAMPAIGN_ID, SEQ)" in upper
    assert upper.count("BEFORE UPDATE OR DELETE OR TRUNCATE") == 3  # campaigns, attempts, outcomes
    assert "FOREIGN KEY (CAMPAIGN_ID, SEQ) REFERENCES API_FOOTBALL_CERT_CAMPAIGN_ATTEMPTS" in upper


def test_the_runtime_never_applies_migrations():
    """Nothing in the deployed code calls the migration runner: 0010 is never applied at deploy."""
    offenders = []
    for path in sorted(list((ROOT / "bridge").rglob("*.py")) + list((ROOT / "app").rglob("*.py"))):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\b(apply_migrations|migrate)\(", text):
            line = text[: match.start()].count("\n") + 1
            source = text.splitlines()[line - 1].strip()
            if source.startswith("def ") or path.name == "migrations.py":
                continue
            if path.name == "db.py" and source == "apply_migrations(connect)":
                continue  # inside app.db.migrate(), itself never called by the runtime
            offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert offenders == []
    for start in ("Procfile", "render.yaml", "Dockerfile"):
        p = ROOT / start
        assert not p.exists() or "migrat" not in p.read_text(encoding="utf-8").lower()


def test_generated_0001_0010_script_is_current():
    out = subprocess.run([sys.executable, "scripts/generate_aiven_migrations.py", "--target", "bridge-0001-0010", "--check"],
                         cwd=ROOT, capture_output=True, text=True, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr[-500:]
    text = SCRIPT.read_text(encoding="utf-8")
    assert "0010_apifootball_cert_campaign_ledger.sql" in text
    for marker in ("signer_ledger_anchors", "signer_ledger_writer_leases", "signer_schema_migrations"):
        assert marker not in text


def test_workflow_is_manual_owner_gated_pinned_and_backs_up_first():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"^on:\n  workflow_dispatch:", text, re.M)
    for forbidden in ("pull_request", "push:", "schedule:", "workflow_run"):
        assert forbidden not in text
    assert "environment: aiven-production" in text and "if: github.ref == 'refs/heads/main'" in text
    assert "require_protected_environment.sh" in text and "require_owner_main_dispatch.sh" in text
    assert f"CONFIRM_PHRASE: {PHRASE}" in text and "default: diagnose" in text
    assert text.count(f"inputs.confirm == '{PHRASE}'") == 3
    pins = dict(line.strip().split("=", 1) for line in
                text.split("EXPECTED_SQL_SHA256: |", 1)[1].split("PGCONNECT_TIMEOUT", 1)[0].strip().splitlines())
    assert pins == {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (SCRIPT, REQUIRE_0009, GRANT)}
    assert "--target bridge-0001-0010 --check" in text
    # Precheck: 0001-0009 (apply) or 0001-0010 (no-op); a database without 0009 is refused.
    assert 'case "$state" in BRIDGE_0001_0009|BRIDGE_0001_0010) ;;' in text
    assert text.index("--schema-only") < text.index("- name: Apply bridge 0010")
    assert "run;    first=" in text and "run;    second=" in text
    assert ('-c "$GUARD" -f sql/aiven/apifootball_cert_ledger_0010_require_0009.sql -f "$SCRIPT" '
            '-f sql/aiven/apifootball_cert_ledger_0010_grant.sql' in text)  # AF-11: 0009 checked in the transaction
    assert "oauth_throttle" not in text


def test_grant_is_select_insert_only_and_asserted():
    code = _code(GRANT.read_text(encoding="utf-8")).upper()
    assert ("GRANT SELECT, INSERT ON TABLE PUBLIC.API_FOOTBALL_CERT_CAMPAIGNS, PUBLIC.API_FOOTBALL_CERT_CAMPAIGN_ATTEMPTS, "
            "PUBLIC.API_FOOTBALL_CERT_CAMPAIGN_OUTCOMES TO FCB_APP") in code
    assert code.count("GRANT ") == 1
    assert "UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER" in code and "PG_HAS_ROLE('FCB_APP', 'FCB_MIGRATOR', 'MEMBER')" in code
    assert "FCB_CERT_LEDGER_GRANTS_UNEXPECTED" in code and "FCB_CERT_LEDGER_NOT_MIGRATOR" in code
    assert "API_FOOTBALL_QUOTA" not in code


def test_0010_transaction_requires_0009_with_the_repository_checksum():
    """AF-11: the bridge 0001-0010 script would also apply a missing 0009; this guard runs first."""
    code = _code(REQUIRE_0009.read_text(encoding="utf-8"))
    assert f"'{SHA256_0009}'" in code and "'0009_oauth_throttle_buckets.sql'" in code
    assert "FCB_CERT_LEDGER_REQUIRES_BRIDGE_0009" in code and "OPERATOR(pg_catalog.>) 10" in code
    for word in ("INSERT", "UPDATE", "DELETE", "GRANT", "CREATE", "ALTER", "DROP"):
        assert word not in code.upper().replace("RAISE EXCEPTION", ""), word


def test_wp009_grant_files_carry_the_same_conditional_select_insert_ledger_grant():
    """AF-13 (declared intentional change of the WP-009 grant files and their pins): a re-run of the
    roles workflow keeps S I on the ledger where 0010 exists and changes nothing where it does not."""
    blocks = []
    for path in WP009_FILES:
        text = path.read_text(encoding="utf-8")
        blocks.append(text[text.index("DO $certledger$"):text.index("END $certledger$;")])
    assert blocks[0] == blocks[1]
    block = blocks[0]
    for table in LEDGER:
        assert f"pg_catalog.to_regclass('public.{table}') IS NOT NULL" in block
    granted = re.sub(r"'\s*'", "", " ".join(block.split()))
    assert ("GRANT SELECT, INSERT ON TABLE public.api_football_cert_campaigns, public.api_football_cert_campaign_attempts, "
            "public.api_football_cert_campaign_outcomes TO fcb_app") in granted
    for word in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "ALL "):
        assert word not in _code(block).upper().split("GRANT", 1)[1].split("TO FCB_APP")[0], word
    roles = (ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-roles.yml").read_text(encoding="utf-8")
    for path in WP009_FILES:
        assert f"{path.relative_to(ROOT)}={hashlib.sha256(path.read_bytes()).hexdigest()}" in roles
