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
    first = STEPS[0]
    assert "required_reviewers" in first["run"] and "exit 1" in first["run"]
    assert "secrets." not in yaml.safe_dump(first)
    secret_steps = [i for i, s in enumerate(STEPS) if "secrets.FCB_DEFAULTDB_URL" in yaml.safe_dump(s)]
    assert secret_steps and min(secret_steps) > 1  # protection and confirmation gates come first


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
                                  "verify_bridge_schema.sql"])
def test_precheck_and_verify_files_are_read_only(name):
    sql = (ROOT / "sql" / "aiven" / name).read_text(encoding="utf-8").upper()
    code = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    for word in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "CREATE ", "TRUNCATE", "GRANT ", "COPY "):
        assert word not in code, (name, word)


def test_diag_url_variable_is_allowlisted():
    sys.path.insert(0, str(ROOT / "scripts"))
    import aiven_pg18_diag as diag
    assert diag.url_var({}) == "FCB_PG18_TEST_URL"
    assert diag.url_var({"FCB_DB_URL_VAR": "FCB_DEFAULTDB_URL"}) == "FCB_DEFAULTDB_URL"
    assert diag.url_var({"FCB_DB_URL_VAR": "PATH"}) is None
