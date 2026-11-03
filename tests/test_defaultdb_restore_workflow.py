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


def test_protection_gate_passes_with_a_required_reviewer(tmp_path):
    out = _gate(tmp_path, '{"protection_rules":[{"type":"required_reviewers","reviewers":[{"type":"User"}]}]}')
    assert out.returncode == 0 and out.stdout.strip() == "1"


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
    assert "pg_advisory_xact_lock(460244120)" in guard and "FCB_RESTORE_STATE_REFUSED" in guard
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
    assert "FCB_TARGET_GUARD_FAILED" in restore and "current_database() <> '$EXPECTED_DB'" in restore
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
    for m in migrations.load_migrations():
        assert m.name in text


def test_tracked_precheck_pins_exactly_the_bridge_files():
    sql = (ROOT / "sql" / "aiven" / "defaultdb_precheck_tracked.sql").read_text(encoding="utf-8")
    rows = re.findall(r"\((\d+), '([^']+)', '([0-9a-f]{64})'\)", sql)
    assert [(int(v), n, c) for v, n, c in rows] == [(m.version, m.name, m.checksum)
                                                    for m in migrations.load_migrations()]


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
    assert "(pg_catalog.current_schemas(true))[1] <> 'pg_catalog'" in guard
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
