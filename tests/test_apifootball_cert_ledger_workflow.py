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
LEDGER = ("api_football_cert_plans", "api_football_cert_campaigns", "api_football_cert_campaign_attempts",
          "api_football_cert_campaign_outcomes")
APPROVE_WORKFLOW = ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-apifootball-cert-plan-approve.yml"
APPROVE_SQL = ROOT / "sql" / "aiven" / "apifootball_cert_plan_approve.sql"
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
    # Declared (review #199 P1): the only data writes are the plan counter's own, inside trigger
    # functions (counter row created with its plan; +1 per reservation). Nothing else is written.
    writes = [line.strip() for line in sql.splitlines()
              if re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", line.upper())
              and not re.search(r"BEFORE (UPDATE|DELETE|INSERT)|AFTER INSERT|INSERT OR UPDATE", line.upper())]
    assert writes == ["EXECUTE pg_catalog.format('INSERT INTO %I.api_football_cert_plan_usage (plan_sha256, used) VALUES ($1, 0)',",
                      "EXECUTE pg_catalog.format('UPDATE %I.api_football_cert_plan_usage u SET used = u.used OPERATOR(pg_catalog.+) 1 '"], writes
    for word in ("DROP ", "ALTER ", "GRANT ", "REVOKE ", "CREATE ROLE", "SIGNER"):
        assert word not in upper, word
    statements = [s.strip() for s in re.split(r";\s*\n(?=CREATE|$)", sql) if s.strip()]
    assert all(s.upper().startswith(("CREATE TABLE IF NOT EXISTS", "CREATE FUNCTION", "CREATE TRIGGER"))
               for s in statements), statements
    assert "CHECK (BUDGET BETWEEN 1 AND 78)" in upper and "CHECK (SEQ BETWEEN 1 AND 78)" in upper
    assert "PRIMARY KEY (CAMPAIGN_ID, SEQ)" in upper
    assert upper.count("BEFORE UPDATE OR DELETE OR TRUNCATE") == 4  # plans, campaigns, attempts, outcomes
    assert "PLAN_SHA256 TEXT NOT NULL REFERENCES API_FOOTBALL_CERT_PLANS (PLAN_SHA256)" in upper  # AF-15
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


WRITABLE = {  # review #199 P3-2: INSERT is column-level, never on an audit timestamp
    "api_football_cert_campaigns": ("campaign_id", "plan_sha256"),
    "api_football_cert_campaign_attempts": ("campaign_id", "call_id"),  # seq: assigned by the server
    "api_football_cert_campaign_outcomes": ("campaign_id", "seq", "outcome", "http_status"),
}
AUDIT_COLUMNS = ("created_at", "reserved_at", "recorded_at")


def _grants(text):
    flat = re.sub(r"'\s*'", "", text).replace("'", ";")
    return [g for g in (" ".join(x.split()) for x in flat.split(";")) if g.startswith("GRANT ")]


EXPECTED_GRANTS = ["GRANT SELECT ON TABLE public.api_football_cert_plans, public.api_football_cert_campaigns, "
                   "public.api_football_cert_campaign_attempts, public.api_football_cert_campaign_outcomes TO fcb_app"] + [
    f"GRANT INSERT ({', '.join(cols)}) ON TABLE public.{t} TO fcb_app" for t, cols in WRITABLE.items()]


def test_grant_is_select_plus_column_insert_and_asserted():
    code = _code(GRANT.read_text(encoding="utf-8"))
    assert _grants(code) == EXPECTED_GRANTS
    for column in AUDIT_COLUMNS:
        assert all(column not in g for g in _grants(code))
    upper = code.upper()
    assert "INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER" in upper  # no table-level INSERT either
    assert "HAS_COLUMN_PRIVILEGE('FCB_APP', A.ATTRELID, A.ATTNUM, 'INSERT')" in upper
    for cols in WRITABLE.values():
        assert f"'{','.join(cols)}'" in code
    assert "ARRAY['', 'campaign_id,plan_sha256'" in code  # AF-15: no INSERT column at all on plans
    assert "PG_HAS_ROLE('FCB_APP', 'FCB_MIGRATOR', 'MEMBER')" in upper
    assert "FCB_CERT_LEDGER_GRANTS_UNEXPECTED" in upper and "FCB_CERT_LEDGER_NOT_MIGRATOR" in upper
    assert "API_FOOTBALL_QUOTA" not in upper
    assert "public.api_football_cert_stamp()" in code  # every 0010 function: PUBLIC EXECUTE revoked


def test_0010_functions_pin_search_path_and_never_resolve_tables_through_the_caller():
    """Review #199 P3-1: no table lookup through the caller's search_path (pg_temp shadowing)."""
    sql = _code(migrations.load_migrations()[9].sql)
    functions = re.findall(r"CREATE FUNCTION (\w+)\(\) RETURNS trigger LANGUAGE plpgsql\s+"
                           r"SET search_path = pg_catalog, pg_temp AS \$\$(.*?)\$\$;", sql, re.S)
    functions = re.findall(r"CREATE FUNCTION (\w+)\(\) RETURNS trigger LANGUAGE plpgsql(?: SECURITY DEFINER)?\s+"
                           r"SET search_path = pg_catalog, pg_temp AS \$\$(.*?)\$\$;", sql, re.S)
    assert sorted(n for n, _ in functions) == ["api_football_cert_attempt_guard", "api_football_cert_plan_usage_rule",
                                               "api_football_cert_reject_mutation", "api_football_cert_stamp"]
    assert sql.count("CREATE FUNCTION") == 4 and sql.count("SECURITY DEFINER") == 1
    for _name, body in functions:
        for table in LEDGER:
            for m in re.finditer(r"(?:FROM|INTO|UPDATE)\s+\S*" + table, body):
                assert "%I." + table in m.group(0), m.group(0)
    assert sql.count("FOR EACH ROW EXECUTE FUNCTION api_football_cert_stamp()") == 4


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
    assert _grants(_code(block)) == EXPECTED_GRANTS
    for word in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "ALL ", *AUDIT_COLUMNS):
        assert all(word.upper() not in g.upper() for g in _grants(_code(block))), word
    for path in WP009_FILES:  # intentional change (WP-009 compatibility): the limit is lifted, the comment says so
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "The roles workflow admits the 0001-0009 and 0001-0010" in text
    roles = (ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-roles.yml").read_text(encoding="utf-8")
    for path in WP009_FILES:
        assert f"{path.relative_to(ROOT)}={hashlib.sha256(path.read_bytes()).hexdigest()}" in roles


def test_readiness_defers_exactly_the_tables_0010_creates():
    """Review #199 P3-3 (and AF-15, P1): every 0010 table (5/5, the plan counter included) is always
    deferred and required once 0010 is recorded."""
    from bridge import schema_readiness
    created = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", migrations.load_migrations()[9].sql)
    assert set(schema_readiness.ALWAYS_DEFERRED) == {10}
    assert sorted(schema_readiness.ALWAYS_DEFERRED[10]) == sorted(created) == sorted(LEDGER + ("api_football_cert_plan_usage",))


def test_p1_budget_is_a_per_plan_counter_incremented_by_a_security_definer_trigger():
    """Review #199 P1 (owner decision): isolation-independent budget, one path, seq = counter."""
    sql = _code(migrations.load_migrations()[9].sql)
    guard = sql[sql.index("CREATE FUNCTION api_football_cert_attempt_guard"):]
    assert guard.startswith("CREATE FUNCTION api_football_cert_attempt_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER")
    assert "u.used OPERATOR(pg_catalog.<) p.budget RETURNING u.used" in guard and "NEW.seq := numbered;" in guard
    assert "API_FOOTBALL_CERT_GUARD_MISUSE" in guard and "TG_RELID" in guard and "CURRENT_USER" in guard
    assert "advisory" not in sql.lower() and "transaction_isolation" not in sql
    assert "API_FOOTBALL_CERT_PLAN_NOT_APPROVED" in guard and "API_FOOTBALL_CERT_CAMPAIGN_BUDGET_EXHAUSTED" in guard
    rule = sql[sql.index("CREATE FUNCTION api_football_cert_plan_usage_rule"):sql.index("CREATE TRIGGER api_football_cert_plans_usage_init")]
    assert "NEW.used OPERATOR(pg_catalog.<>) (OLD.used OPERATOR(pg_catalog.+) 1)" in rule  # only ever +1
    assert "BEFORE DELETE OR TRUNCATE\n    ON api_football_cert_plan_usage" in sql
    assert "CHECK (PLAN_KIND <> 'LIVE_FULL_MATCH_SNAPSHOT' OR BUDGET <= 8)" in sql.upper()
    grant = _code(GRANT.read_text(encoding="utf-8"))
    assert "api_football_cert_plan_usage" in grant and all("plan_usage" not in g for g in _grants(grant))
    assert "'search_path=pg_catalog, pg_temp'" in grant and "FCB_CERT_LEDGER_FUNCTIONS_UNEXPECTED" in grant


def test_af15_plan_approval_workflow_is_owner_gated_pinned_and_binds_the_committed_plan():
    text = APPROVE_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"^on:\n  workflow_dispatch:", text, re.M)
    for forbidden in ("pull_request", "push:", "schedule:", "workflow_run"):
        assert forbidden not in text
    assert "environment: aiven-production" in text and "if: github.ref == 'refs/heads/main'" in text
    assert "require_protected_environment.sh" in text and "require_owner_main_dispatch.sh" in text
    phrase = "APPROVE API-FOOTBALL CERT PLAN ON defaultdb"
    assert f"CONFIRM_PHRASE: {phrase}" in text and "default: diagnose" in text
    assert f"inputs.mode == 'approve' && inputs.confirm == '{phrase}'" in text
    assert f"sql/aiven/apifootball_cert_plan_approve.sql={hashlib.sha256(APPROVE_SQL.read_bytes()).hexdigest()}" in text
    assert "--check-plan" in text and '--plan-approval-request "$PLAN"' in text
    assert 'test "$sha" = "$PLAN_SHA256_INPUT"' in text  # input must be the committed plan
    assert 'test "$rows" = "$files10"' in text  # 0010 recorded
    assert "APPROVED_BY: ${{ github.actor }}" in text and "RUN_ID: ${{ github.run_id }}" in text


def test_af15_approval_sql_is_append_only_and_never_renews_a_plan():
    code = _code(APPROVE_SQL.read_text(encoding="utf-8"))
    assert "ON CONFLICT (plan_sha256) DO NOTHING" in code and "FCB_CERT_PLAN_CONFLICT" in code
    assert "FCB_CERT_PLAN_NOT_MIGRATOR" in code and "FCB_CERT_PLAN_INPUT_INVALID" in code
    for word in ("UPDATE public", "DELETE FROM", "TRUNCATE ", "GRANT ", "ALTER ", "DROP "):
        assert word not in code, word
