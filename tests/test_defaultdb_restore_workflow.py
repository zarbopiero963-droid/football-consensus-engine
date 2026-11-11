"""FCB-WP-005: static gates of the owner-gated defaultdb restore workflow (no network, no DB)."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app import migrations

ROOT = Path(__file__).resolve().parents[1]
WF_PATH = ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-restore.yml"
WF = yaml.safe_load(WF_PATH.read_text(encoding="utf-8"))
JOB = WF["jobs"]["restore"]
STEPS = JOB["steps"]
PHRASE = "APPLY BRIDGE 0001-0008 TO defaultdb"


def _index(predicate):
    return next(i for i, s in enumerate(STEPS) if predicate(s))


def test_manual_only_with_diagnose_default():
    on = WF.get("on", WF.get(True))
    assert set(on) == {"workflow_dispatch"}
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["default"] == "diagnose" and inputs["mode"]["options"] == ["diagnose", "apply"]
    assert inputs["confirm"]["default"] == ""


def test_runs_in_the_protected_environment_with_read_only_token():
    assert JOB["environment"] == "aiven-production"
    assert WF["permissions"] == {"contents": "read", "actions": "read"}
    assert JOB["env"]["EXPECTED_DB"] == "defaultdb"
    assert JOB["env"]["CONFIRM_PHRASE"] == PHRASE


def test_environment_protection_is_checked_before_anything_else():
    gate_idx = _index(lambda s: "protected environment" in s.get("name", ""))
    gate = STEPS[gate_idx]
    assert "scripts/require_protected_environment.sh" in gate["run"]
    assert "|| echo" not in gate["run"]  # review P1: no fallback value that can fail open
    assert "secrets." not in yaml.safe_dump(gate)
    assert all("uses" in s for s in STEPS[:gate_idx])  # only the checkout runs before it
    secret_steps = [i for i, s in enumerate(STEPS) if "secrets.FCB_DEFAULTDB_URL" in yaml.safe_dump(s)]
    confirm_idx = _index(lambda s: s.get("name") == "Mode and confirmation gate")
    assert secret_steps and min(secret_steps) > max(gate_idx, confirm_idx)


def _gate(tmp_path, gh_body, gh_rc=0):
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "gh").write_text(f"#!/usr/bin/env bash\nprintf '%s' '{gh_body}'\nexit {gh_rc}\n")
    (stub / "gh").chmod(0o755)
    import os
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
    return subprocess.run(["bash", str(ROOT / "scripts" / "require_protected_environment.sh"), "o/r",
                           "aiven-production"], env=env, capture_output=True, text=True)


@pytest.mark.parametrize("body,rc", [
    ('{"message":"Access to this GitHub API path is not permitted"}', 1),  # review P1 repro
    ('{"message":"Not Found"}', 1),
    ('not json', 0),
    ('{"protection_rules":[]}', 0),
    ('{"protection_rules":[{"type":"wait_timer","wait_timer":5}]}', 0),
    ('{"protection_rules":[{"type":"required_reviewers","reviewers":[]}]}', 0),
    ('{}', 0),
])
def test_protection_gate_fails_closed(tmp_path, body, rc):
    out = _gate(tmp_path, body, rc)
    assert out.returncode != 0
    assert out.stdout == ""


def test_protection_gate_passes_with_a_required_reviewer_and_a_branch_policy(tmp_path):
    # G-02 (declared behaviour change): a deployment-branch policy is now required as well;
    # the missing/wide-policy refusals are covered in tests/test_workflow_secret_policy.py.
    out = _gate(tmp_path, '{"protection_rules":[{"type":"required_reviewers","reviewers":[{"type":"User"}]}],'
                          '"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true},'
                          '"total_count":1,"branch_policies":[{"name":"main","type":"branch"}]}')
    assert out.returncode == 0 and out.stdout.strip() == "1"


def test_protection_gate_refuses_a_reviewer_without_a_branch_policy(tmp_path):
    out = _gate(tmp_path, '{"protection_rules":[{"type":"required_reviewers","reviewers":[{"type":"User"}]}]}')
    assert out.returncode != 0 and out.stdout == ""


def test_apply_runs_guard_script_and_count_assertion_in_one_transaction():
    restore = STEPS[_index(lambda s: "Restore bridge 0001-0008" in s.get("name", ""))]["run"]
    run_line = restore[restore.index("run() {"):restore.index("verify() {")]
    order = [run_line.index(x) for x in ('-1 -c "$GUARD"', "defaultdb_restore_guard.sql", '-f "$SCRIPT"',
                                         "assert_bridge_counts.sql")]
    assert order == sorted(order)
    assert '-v expected_counts="$EXPECTED_COUNTS"' in run_line


def test_restore_guard_is_generated_from_the_prechecks():
    out = subprocess.run([sys.executable, "scripts/generate_aiven_migrations.py", "--target", "restore-guard",
                          "--check"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    guard = (ROOT / "sql" / "aiven" / "defaultdb_restore_guard.sql").read_text(encoding="utf-8")
    # audit F4 (declared test change): the int8 overload, schema-qualified
    assert "SELECT pg_catalog.pg_advisory_xact_lock(460244120::pg_catalog.int8);" in guard
    assert "FCB_RESTORE_STATE_REFUSED" in guard
    assert "REFUSE_RUNTIME_TABLE_DRIFT" in guard and "REFUSE_PARTIAL_OR_DRIFT" in guard


def test_only_the_apply_step_writes_and_it_needs_mode_and_phrase():
    apply_idx = _index(lambda s: "Restore bridge 0001-0008" in s.get("name", ""))
    cond = STEPS[apply_idx]["if"]
    assert "inputs.mode == 'apply'" in cond and f"inputs.confirm == '{PHRASE}'" in cond
    for i, step in enumerate(STEPS):
        run = step.get("run", "")
        if i != apply_idx:
            assert '-f "$SCRIPT"' not in run and "-1 -c" not in run, step.get("name")
    gate = STEPS[_index(lambda s: s.get("name") == "Mode and confirmation gate")]["run"]
    assert '"$CONFIRM" != "$CONFIRM_PHRASE"' in gate
    restore = STEPS[apply_idx]["run"]
    assert "FCB_TARGET_GUARD_FAILED" in restore
    assert "pg_catalog.current_database() OPERATOR(pg_catalog.<>) '$EXPECTED_DB'::pg_catalog.name" in restore
    assert "state changed since precheck" in restore  # re-check right before writing


def test_never_touches_the_test_database_secret_or_the_signer_series():
    text = WF_PATH.read_text(encoding="utf-8")
    assert "FCB_PG18_TEST_URL" not in text and "fcb_pg18_test" not in text
    assert "series=SIGNER" not in text and "sql/migrations/signer" not in text
    assert not re.search(r"echo[^\n]*\$\{?FCB_DEFAULTDB_URL", text)
    assert "set -x" not in text


def test_pinned_script_is_the_generated_bridge_series():
    script = ROOT / JOB["env"]["SCRIPT"]
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    assert digest == JOB["env"]["EXPECTED_SCRIPT_SHA256"]
    out = subprocess.run([sys.executable, "scripts/generate_aiven_migrations.py", "--target", "bridge-0001-0008",
                          "--check"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == digest
    text = script.read_text(encoding="utf-8")
    for marker in ("signer_ledger_anchors", "signer_ledger_writer_leases", "signer_schema_migrations"):
        assert marker not in text
    # The restore script is exactly 0001-0008; 0009+ has its own owner-gated workflow
    # (intentional change with the OAuth persistent throttle).
    for m in migrations.load_migrations()[:8]:
        assert m.name in text
    for m in migrations.load_migrations()[8:]:
        assert m.name not in text


def test_tracked_precheck_pins_exactly_the_bridge_files():
    sql = (ROOT / "sql" / "aiven" / "defaultdb_precheck_tracked.sql").read_text(encoding="utf-8")
    rows = re.findall(r"\((\d+), '([^']+)', '([0-9a-f]{64})'\)", sql)
    assert [(int(v), n, c) for v, n, c in rows] == [(m.version, m.name, m.checksum)
                                                    for m in migrations.load_migrations()[:8]]  # 0001-0008 only


@pytest.mark.parametrize("name", ["defaultdb_precheck.sql", "defaultdb_precheck_tracked.sql",
                                  "verify_bridge_schema.sql", "defaultdb_restore_guard.sql",
                                  "assert_bridge_counts.sql", "defaultdb_restore_post_guard.sql",
                                  "fingerprint_bridge_schema.sql"])
def test_precheck_and_verify_files_are_read_only(name):
    sql = (ROOT / "sql" / "aiven" / name).read_text(encoding="utf-8").upper()
    code = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    code = re.sub(r"'(?:[^']|'')*'", "''", code)  # string literals (expected signatures) are data
    for word in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "CREATE ", "TRUNCATE", "GRANT ", "COPY "):
        assert word not in code, (name, word)


def test_diag_url_variable_is_allowlisted():
    sys.path.insert(0, str(ROOT / "scripts"))
    import aiven_pg18_diag as diag
    assert diag.url_var({}) == "FCB_PG18_TEST_URL"
    assert diag.url_var({"FCB_DB_URL_VAR": "FCB_DEFAULTDB_URL"}) == "FCB_DEFAULTDB_URL"
    assert diag.url_var({"FCB_DB_URL_VAR": "PATH"}) is None


def test_only_the_repository_owner_can_start_or_rerun_it():
    gate = STEPS[_index(lambda s: s.get("name") == "Mode and confirmation gate")]
    assert gate["env"]["TRIGGERING_ACTOR"] == "${{ github.triggering_actor }}"
    assert gate["env"]["REPO_OWNER"] == "${{ github.repository_owner }}"
    assert '"$TRIGGERING_ACTOR" != "$REPO_OWNER"' in gate["run"] and '"$ACTOR" != "$REPO_OWNER"' in gate["run"]


def test_runs_only_from_main_and_the_target_guard_pins_the_search_path():
    gate = STEPS[_index(lambda s: s.get("name") == "Mode and confirmation gate")]
    assert gate["env"]["GIT_REF"] == "${{ github.ref }}" and '"$GIT_REF" != "refs/heads/main"' in gate["run"]
    restore = STEPS[_index(lambda s: "Restore bridge 0001-0008" in s.get("name", ""))]["run"]
    guard = next(l for l in restore.splitlines() if l.strip().startswith('GUARD="DO'))
    assert "(pg_catalog.current_schemas(true))[1] OPERATOR(pg_catalog.<>) 'pg_catalog'::pg_catalog.name" in guard
    assert "pg_catalog.pg_namespace" in guard  # foreign schemas re-checked inside the transaction
    for fn in ("current_database()", "current_setting(", "pg_backend_pid()"):
        assert f"pg_catalog.{fn}" in guard, fn


def test_post_guard_runs_last_in_the_transaction_with_the_pinned_fingerprint():
    restore = STEPS[_index(lambda s: "Restore bridge 0001-0008" in s.get("name", ""))]["run"]
    run_line = restore[restore.index("run() {"):restore.index("verify() {")]
    assert run_line.index("assert_bridge_counts.sql") < run_line.index("defaultdb_restore_post_guard.sql")
    assert '-v expected_fingerprint="$EXPECTED_FINGERPRINT"' in run_line
    assert re.fullmatch(r"[0-9a-f]{32}", JOB["env"]["EXPECTED_FINGERPRINT"])
    out = subprocess.run([sys.executable, "scripts/generate_aiven_migrations.py", "--target", "restore-post-guard",
                          "--check"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    post = (ROOT / "sql" / "aiven" / "defaultdb_restore_post_guard.sql").read_text(encoding="utf-8")
    assert "FCB_RESTORE_POST_STATE" in post and "FCB_RESTORE_FINGERPRINT_MISMATCH" in post


def test_psql_steps_force_the_search_path_and_the_target_check_step_does_not():
    for name in ("Prechecks (database, PostgreSQL 18, TLS, restore state)",
                 "Restore bridge 0001-0008 (single transaction, run twice) and verify"):
        step = STEPS[_index(lambda s, n=name: s.get("name") == n)]
        assert step["env"]["PGOPTIONS"] == "-c search_path=public -c quote_all_identifiers=off", name
    mask = STEPS[_index(lambda s: s.get("name") == "Mask connection details and enforce TLS")]
    assert "PGOPTIONS" not in mask.get("env", {})  # the target check refuses redirecting libpq env


def test_production_restore_requires_verify_full_and_authenticated_ca():
    text = WF_PATH.read_text(encoding="utf-8")
    assert "FCB_AIVEN_CA_PEM" in text
    assert "FCB_AIVEN_CA_SHA256" in text
    assert "PGSSLMODE: verify-full" in text
    assert "PGSSLROOTCERT" in text
    assert "sslmode must be verify-full" in text
    assert "sslmode=require" not in text


def test_diagnose_emits_a_sanitized_read_only_inventory_in_the_downloadable_log():
    step = STEPS[_index(lambda s: s.get("name", "").startswith("Sanitized first-install inventory"))]
    assert step["if"] == "${{ inputs.mode == 'diagnose' }}"
    assert step["env"]["FCB_DEFAULTDB_URL"] == "${{ secrets.FCB_DEFAULTDB_URL }}"
    assert step["env"]["PGSSLMODE"] == "verify-full"
    run = step["run"]
    assert "scripts/defaultdb_first_init_inventory.py" in run
    assert "FIRST_INIT_INVENTORY=" in run
    assert "GITHUB_STEP_SUMMARY" in run
    assert "apply" not in run.lower()


# ---------------------------------------------------------------- audit of 2026-11-09 (F1-F8)

sys.path.insert(0, str(ROOT / "scripts"))
import generate_aiven_migrations as gen  # noqa: E402

AIVEN = ROOT / "sql" / "aiven"
GUARDED_SQL = ["defaultdb_precheck.sql", "defaultdb_precheck_tracked.sql", "fingerprint_bridge_schema.sql",
               "verify_bridge_schema.sql", "assert_bridge_counts.sql", "defaultdb_restore_guard.sql",
               "defaultdb_restore_post_guard.sql"]
IN_TRANSACTION = ["defaultdb_restore_guard.sql", "assert_bridge_counts.sql", "defaultdb_restore_post_guard.sql"]
# SQL keywords that take a parenthesis, and the CTE / alias column lists of these files
_PAREN_WORDS = {"coalesce", "exists", "any", "all", "values", "as", "and", "or", "not", "on", "from", "join",
                "select", "where", "when", "then", "else", "if", "case", "array", "union", "except", "in", "into",
                "expected", "bridge_functions", "bridge_triggers", "runtime_tables", "runtime_rel", "got", "lines",
                "cfg"}


def _bash_unescape(text):
    return text.replace("\\$", "$")


def _restore_guard_text():
    restore = STEPS[_index(lambda s: "Restore bridge 0001-0008" in s.get("name", ""))]["run"]
    line = next(l.strip() for l in restore.splitlines() if l.strip().startswith('GUARD="DO'))
    return _bash_unescape(line[len('GUARD="'):-1])


def _precheck_schema_query():
    pre = STEPS[_index(lambda s: s.get("name", "").startswith("Prechecks"))]["run"]
    line = next(l.strip() for l in pre.splitlines() if l.strip().startswith('schemas="'))
    return _bash_unescape(line[len('schemas="$(q "'):-len('")"')])


def qualification_findings(sql_text):
    """Every catalog relation, function, operator and cast type must be schema-qualified."""
    code = "\n".join(l for l in sql_text.splitlines() if not l.lstrip().startswith("--"))
    code = re.sub(r"E?'(?:[^']|'')*'", "''", code)          # literals are data
    code = re.sub(r"--[^\n]*", "", code)
    code = re.sub(r"\$[a-z_]*\$", " ", code)                  # dollar-quote delimiters
    code = code.replace("SET LOCAL search_path = public;", "")
    code = code.replace(":=", " ")                            # PL/pgSQL assignment
    code = re.sub(r":''", " ", code)                          # psql :'variables'
    findings = []
    stripped = re.sub(r"OPERATOR\(pg_catalog\.[^)\s]+\)", " ", code)
    for m in re.finditer(r"[=<>|~!+]", stripped):
        findings.append(("bare operator", stripped[max(0, m.start() - 40):m.end() + 10]))
    for m in re.finditer(r"(?<![\w.])(pg_\w+)", code):
        if m.group(1) != "pg_catalog":
            findings.append(("unqualified pg_ name", m.group(1)))
    for m in re.finditer(r"(?<![\w.$])([A-Za-z_]\w*)\s*\(", stripped):
        if m.group(1).lower() not in _PAREN_WORDS:
            findings.append(("unqualified call", m.group(1)))
    for m in re.finditer(r"::\s*(?!pg_catalog\.)(\S+)", code):
        findings.append(("unqualified cast", m.group(1)))
    for m in re.finditer(r"(?i)\b(NOT\s+IN|IN\s*\(|LIKE|BETWEEN|IS\s+(?:NOT\s+)?DISTINCT|NULLIF|USING\s*\(|"
                         r"to_regclass|quote_ident)", code):
        findings.append(("implicit operator lookup", m.group(1)))
    for m in re.finditer(r"(?i)\bCASE\s+(?=\S)(?!WHEN\b)", code):
        findings.append(("simple CASE", m.group(0)))
    for m in re.finditer(r"(?<![\w.])(schema_migrations|signer_schema_migrations)\b", code):
        findings.append(("unqualified relation", m.group(1)))
    return findings


@pytest.mark.parametrize("name", GUARDED_SQL)
def test_guard_and_evidence_sql_is_fully_schema_qualified(name):
    """Audit F3/F4: nothing in public (or a schema put first mid-transaction) can be resolved."""
    assert qualification_findings((AIVEN / name).read_text(encoding="utf-8")) == []


def test_workflow_guard_and_schema_precheck_are_fully_schema_qualified():
    guard = _restore_guard_text().replace("$EXPECTED_DB", "defaultdb")
    assert qualification_findings(guard) == []
    assert qualification_findings(_precheck_schema_query()) == []


@pytest.mark.parametrize("bad", [
    "SELECT count(*) FROM pg_catalog.pg_class",
    "SELECT 1 FROM pg_class",
    "SELECT 1 WHERE 'a'::text = 'b'::pg_catalog.text",
    "SELECT 1 WHERE x::text OPERATOR(pg_catalog.=) y",
    "SELECT 1 WHERE a NOT IN (SELECT b)",
    "SELECT 1 WHERE n LIKE 'pg_temp%'",
    "SELECT to_regclass(quote_ident(x))",
    "SELECT 1 FROM schema_migrations",
    "SELECT a || b",
    "SELECT CASE x WHEN 1 THEN 2 END",
])
def test_the_qualification_lint_catches_each_pattern(bad):
    assert qualification_findings(bad)


def test_workflow_guard_reasserts_the_session_like_the_generated_guards():
    """F1/F3/F5: CURRENT_USER = SESSION_USER, no database/role settings, search_path exactly
    public, anchored temp-schema regex and Aiven's real `_avnadmin_monitor` shape only."""
    guard = _restore_guard_text()
    assert gen.SESSION_TAMPERED in guard
    for clause in ("CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER",
                   "pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) 'public'::pg_catalog.text",
                   "'^pg_(toast_)?temp_[0-9]+$'", "pg_catalog.pg_settings g", "'database user'",
                   "r_owner.rolname OPERATOR(pg_catalog.=) '_avnadmin_monitor'::pg_catalog.name",
                   "pg_catalog.pg_has_role(SESSION_USER, n.nspowner, 'MEMBER')",
                   "pg_catalog.has_schema_privilege(SESSION_USER, n.oid, 'CREATE')"):
        assert clause in gen.SESSION_TAMPERED, clause
    text = WF_PATH.read_text(encoding="utf-8")
    assert "LIKE 'pg_temp" not in text and "like 'pg_temp" not in text
    schema_query = _precheck_schema_query()
    assert gen.UNEXPECTED_SCHEMA_EXISTS.split(" WHERE ", 1)[1].lower().rstrip(")") in schema_query.lower()


@pytest.mark.parametrize("name", IN_TRANSACTION)
def test_in_transaction_files_check_then_pin_the_search_path(name):
    """F3: a search_path (or role) changed by code inside the transaction aborts it; then the path
    is pinned again before any other statement."""
    code = [l for l in (AIVEN / name).read_text(encoding="utf-8").splitlines() if not l.startswith("--")]
    first_statement = "\n".join(code[:code.index("SET LOCAL search_path = public;")])
    assert first_statement.startswith("DO $")
    assert "pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) 'public'::pg_catalog.text" in first_statement
    assert "CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER" in first_statement
    assert "FCB_RESTORE_SESSION_GUARD_FAILED" in first_statement


def test_state_guard_pins_the_0008_fingerprint_and_records_the_prior_state():
    guard = (AIVEN / "defaultdb_restore_guard.sql").read_text(encoding="utf-8")
    lock = guard.index("SELECT pg_catalog.pg_advisory_xact_lock(460244120::pg_catalog.int8);")
    expected = guard.index("pg_catalog.set_config('fcb.expected_fingerprint', :'expected_fingerprint', true)")
    state = guard.index("DO $fcbguard$")
    assert lock < expected < state  # F2: the pinned fingerprint is known before the state guard
    assert "FCB_RESTORE_PRE_FINGERPRINT_MISMATCH" in guard
    assert "pg_catalog.set_config('fcb.prior_state', s, true)" in guard
    post = (AIVEN / "defaultdb_restore_post_guard.sql").read_text(encoding="utf-8")
    assert "FCB_RESTORE_PRIOR_STATE_UNKNOWN" in post and "FCB_RESTORE_FOREIGN_OBJECTS_AFTER_EMPTY" in post
    assert "pg_catalog.pg_current_xact_id()" in post and "x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid" in post


def test_bridge_script_pin_is_unchanged_by_the_audit_fixes():
    """F4 is closed in the guard files only: the generated bridge script and its pin are untouched."""
    assert JOB["env"]["EXPECTED_SCRIPT_SHA256"] == "a6525b5baeade9cad1f3eb5fc27c506199c3d30ccabab5fd57f21eeb01b85a5c"


def test_precheck_pins_exactly_the_bridge_functions_and_triggers():
    """F2: the pinned function bodies (md5) and trigger set are those of sql/migrations/bridge."""
    import hashlib as _h
    sql_text = (AIVEN / "defaultdb_precheck.sql").read_text(encoding="utf-8")
    pinned = dict(re.findall(r"\('([a-z_]+)\|\|trigger\|plpgsql\|false\|-\|([0-9a-f]{32})'\)", sql_text))
    assert set(pinned) == {"canonical_alias_revision_guard", "canonical_reject_mutation",
                           "seal_evaluation_protocol", "seal_pilot_row"}
    bodies = "".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "sql" / "migrations" / "bridge").glob("*.sql")))
    for name, digest in pinned.items():
        found = re.search(r"FUNCTION\s+" + name + r"\(\)\s+RETURNS\s+trigger\s+(?:LANGUAGE\s+plpgsql\s+)?AS\s+\$(\w*)\$(.*?)\$\1\$",
                          bodies, re.S | re.I)
        assert found, name
        assert _h.md5(found.group(2).encode()).hexdigest() == digest, name
    assert len(re.findall(r"\('[a-z_]+\|[a-z_]+\|[a-z_]+\|\d+'\)", sql_text)) == 9
