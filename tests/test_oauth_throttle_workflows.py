"""OAuth persistent throttle: static checks of the two owner-gated workflows (0009 apply and
emergency unlock), their SQL, the generated 0001-0009 script and the owner key-hash helper.
No network. Behaviour of the SQL on real PostgreSQL: tests/integration/test_oauth_throttle_workflow_sql_postgres.py.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app import migrations

ROOT = Path(__file__).resolve().parents[1]
WF_DIR = ROOT / ".github" / "workflows"
APPLY = WF_DIR / "fcb-aiven-defaultdb-oauth-throttle-0009.yml"
UNLOCK = WF_DIR / "fcb-aiven-defaultdb-oauth-throttle-unlock.yml"
AIVEN = ROOT / "sql" / "aiven"
APPLY_PHRASE = "APPLY BRIDGE 0009 OAUTH THROTTLE TO defaultdb"
UNLOCK_PHRASE = "UNLOCK OAUTH THROTTLE ON defaultdb"


def _wf(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    (job,) = data["jobs"].values()
    return data, job, job["steps"]


def _sql_only(text):
    text = re.sub(r"--[^\n]*", "", text)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("\\"))


@pytest.mark.parametrize("path", [APPLY, UNLOCK], ids=lambda p: p.name)
def test_manual_owner_gated_protected_environment_main_only(path):
    data, job, steps = _wf(path)
    triggers = data.get("on", data.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert job["environment"] == "aiven-production" and job["if"] == "github.ref == 'refs/heads/main'"
    assert data["concurrency"]["group"] == "fcb-aiven-defaultdb-restore"
    assert data["permissions"] == {"contents": "read", "actions": "read"}
    assert steps[0]["uses"].startswith("actions/checkout@") and steps[0]["with"]["persist-credentials"] is False
    assert "require_protected_environment.sh" in steps[1]["run"]
    assert "require_owner_main_dispatch.sh" in steps[2]["run"]
    first_secret = next(i for i, s in enumerate(steps) if "secrets." in yaml.safe_dump(s))
    assert first_secret > 2


@pytest.mark.parametrize("path", [APPLY, UNLOCK], ids=lambda p: p.name)
def test_every_sql_file_is_pinned_and_the_pins_match(path):
    data, job, steps = _wf(path)
    pinned = dict(line.strip().split("=") for line in job["env"]["EXPECTED_SQL_SHA256"].splitlines() if line.strip())
    for p, sha in pinned.items():
        assert hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == sha, f"re-pin {p}"
    text = path.read_text(encoding="utf-8").split("steps:", 1)[1]
    used = set(re.findall(r"sql/aiven/[a-z0-9_]+\.sql", text))
    if path is APPLY:
        used.add(job["env"]["SCRIPT"])
    assert used <= set(pinned), sorted(used - set(pinned))
    assert any(f'test "$n" -eq {len(pinned)}' in str(s.get("run", "")) for s in steps)


def test_generated_0001_0009_script_is_committed_and_has_no_signer_series():
    out = subprocess.run([sys.executable, "scripts/generate_aiven_migrations.py", "--target", "bridge-0001-0009",
                          "--check"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    script = (AIVEN / "aiven_migrations_bridge_0001_0009.sql").read_text(encoding="utf-8")
    assert out.stdout.strip() == hashlib.sha256(script.encode()).hexdigest()
    for m in migrations.load_migrations()[:9]:
        assert m.name in script and m.checksum in script
    for marker in ("signer_ledger_anchors", "signer_ledger_writer_leases", "signer_schema_migrations"):
        assert marker not in script
    # The pinned 0001-0008 restore script is unchanged and does not carry 0009.
    out8 = subprocess.run([sys.executable, "scripts/generate_aiven_migrations.py", "--target", "bridge-0001-0008",
                           "--check"], cwd=ROOT, capture_output=True, text=True)
    assert out8.returncode == 0 and out8.stdout.strip() == "a6525b5baeade9cad1f3eb5fc27c506199c3d30ccabab5fd57f21eeb01b85a5c"
    assert "0009_" not in (AIVEN / "aiven_migrations_bridge_0001_0008.sql").read_text()


def test_apply_writes_only_with_the_phrase_after_the_backup_and_diagnose_never_writes():
    _data, job, steps = _wf(APPLY)
    names = [s.get("name", s.get("uses", "")) for s in steps]
    backup = names.index("Backup before any write (schema only, no row data) and migration rows")
    apply_ = next(i for i, n in enumerate(names) if n.startswith("Apply bridge 0009"))
    assert backup < apply_
    cond = f"inputs.mode == 'apply' && inputs.confirm == '{APPLY_PHRASE}'"
    for i, step in enumerate(steps):
        run = str(step.get("run", ""))
        if "-f \"$SCRIPT\"" in run or "oauth_throttle_0009_grant.sql" in run or "pg_dump" in run:
            assert cond in step.get("if", ""), step.get("name")
    assert job["env"]["CONFIRM_PHRASE"] == APPLY_PHRASE
    apply_run = steps[apply_]["run"]
    assert '-1 -c "$GUARD" -f "$SCRIPT" -f sql/aiven/oauth_throttle_0009_grant.sql' in apply_run
    assert "run;    first=" in apply_run and "run;    second=" in apply_run   # second run is a no-op
    assert "--schema-only" in steps[backup]["run"] and "--data-only" not in steps[backup]["run"]
    pre = next(s for s in steps if s.get("name", "").startswith("Prechecks"))["run"]
    assert "BRIDGE_0001_0008|BRIDGE_0001_0009" in pre and 'test "$(q \'select current_user\')" = "fcb_migrator"' in pre


def test_unlock_requires_the_phrase_strict_inputs_and_audits_before_deleting():
    _data, job, steps = _wf(UNLOCK)
    assert job["env"]["CONFIRM_PHRASE"] == UNLOCK_PHRASE
    gate = steps[2]["run"]
    assert 'if [ "$CONFIRM" != "$CONFIRM_PHRASE" ]' in gate
    validate = next(s for s in steps if s.get("name", "").startswith("Validate"))["run"]
    assert "^[a-z0-9]{1,8}:[0-9a-f]{64}$" in validate and "key='*'" in validate
    inputs = yaml.safe_load(UNLOCK.read_text())[True]["workflow_dispatch"]["inputs"]
    assert inputs["scope"]["options"] == ["consent", "registration"] and inputs["target"]["options"] == ["global", "key"]
    sql = _sql_only((AIVEN / "oauth_throttle_unlock.sql").read_text())
    assert sql.index("INSERT INTO public.oauth_throttle_unlock_audit") < sql.index("DELETE FROM public.oauth_throttle_buckets")
    assert "USING audited" in sql and "TRUNCATE" not in sql.upper() and "DROP" not in sql.upper()
    assert "oauth_throttle_unlock_audit" not in sql.split("DELETE FROM", 1)[1].split("USING", 1)[0]
    for var in ("unlock_scope", "unlock_target", "unlock_key_hash", "github_run_id", "github_actor"):
        assert f":'{var}'" in sql and f":{var} " not in sql      # always quoted literals


@pytest.mark.parametrize("name", ["oauth_throttle_unlock.sql", "oauth_throttle_0009_grant.sql"])
def test_workflow_sql_is_catalog_qualified(name):
    sql = re.sub(r"'[^']*'", "''", _sql_only((AIVEN / name).read_text()))
    sql = re.sub(r"(?m)^\s*SET LOCAL [a-z_]+ = [^;]*;$", "", sql)
    for match in re.finditer(r"(?<![\w.])(pg_[a-z_]+|current_setting|set_config|format|unnest|count|"
                             r"to_regclass|has_[a-z_]+_privilege)\s*(?=\()", sql):
        raise AssertionError(f"{name}: unqualified {match.group(1)}")
    for op in (" = ", " <> ", " != ", " || "):
        assert op not in sql, (name, op)


def test_grant_files_carry_the_same_conditional_throttle_grant_and_nothing_on_the_audit():
    a = (AIVEN / "grant_bridge_runtime.sql").read_text()
    b = (AIVEN / "finalize_bridge_privileges.sql").read_text()
    block = a[a.index("DO $throttle$"):a.index("END $throttle$;")]
    assert block and block == b[b.index("DO $throttle$"):b.index("END $throttle$;")]
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.oauth_throttle_buckets TO fcb_app" in block
    assert "pg_catalog.to_regclass('public.oauth_throttle_buckets') IS NOT NULL" in block
    for text in (a, b):
        assert "oauth_throttle_unlock_audit" not in _sql_only(text)
    grant = _sql_only((AIVEN / "oauth_throttle_0009_grant.sql").read_text())
    assert grant.count("GRANT ") == 1 and "oauth_throttle_unlock_audit TO" not in grant


def test_no_workflow_or_config_sets_or_disables_the_throttle():
    """D05/D07: no workflow, deploy file or script sets the mode or the secret."""
    for path in [*WF_DIR.glob("*.y*ml"), *(ROOT / "deploy").rglob("*"), *(ROOT / "scripts").glob("*")]:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert not re.search(r"MCP_OAUTH_THROTTLE_(MODE|KEY)\s*[:=]", text), path


def test_owner_key_hash_helper_reads_the_secret_without_echo_and_prints_only_the_hash(capsys):
    spec = importlib.util.spec_from_file_location("kh", ROOT / "scripts" / "oauth_throttle_key_hash.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import secrets
    secret = "t9:" + secrets.token_urlsafe(40)
    assert mod.main(["x", "203.0.113.5"], read_secret=lambda _p: secret) == 0
    out = capsys.readouterr()
    assert re.fullmatch(r"t9:[0-9a-f]{64}\n", out.out) and secret.split(":", 1)[1] not in out.out + out.err
    assert mod.main(["x", "203.0.113.5"], read_secret=lambda _p: "t9:short") == 1
    assert "getpass" in (ROOT / "scripts" / "oauth_throttle_key_hash.py").read_text()
