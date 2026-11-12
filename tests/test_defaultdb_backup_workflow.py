"""FCB-WP-009 backup: offline structure tests of .github/workflows/fcb-aiven-defaultdb-backup.yml and
unit tests of scripts/fcb_db_backup.py (no database, no network, no secret).

The workflow mirrors the gates of fcb-aiven-defaultdb-roles.yml (protected environment, owner
dispatch from main, exact phrase, pinned target, CA, PGDG key, verify-full, hash-pinned psycopg,
shared concurrency), never reaches a write path (no migration, no apply, no SET ROLE, catalog-only
SQL pinned by SHA-256), and uploads only the ciphertext and its SHA-256 for one day. The end-to-end
behaviour on PostgreSQL 18 is tests/integration/test_defaultdb_backup_postgres.py.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WF_PATH = ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-backup.yml"
ROLES_PATH = ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-roles.yml"
RESTORE_PATH = ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-restore.yml"
SCRIPT = ROOT / "scripts" / "fcb_db_backup.py"
RUNBOOK = ROOT / "docs" / "certification" / "areas" / "defaultdb_backup_runbook.md"
WF = yaml.safe_load(WF_PATH.read_text(encoding="utf-8"))
WF_TEXT = WF_PATH.read_text(encoding="utf-8")
# The workflow without its comment lines (the header describes what it does NOT do).
WF_CODE = "\n".join(l for l in WF_TEXT.splitlines() if not l.lstrip().startswith("#"))
JOB = WF["jobs"]["backup"]
STEPS = JOB["steps"]
ROLES = yaml.safe_load(ROLES_PATH.read_text(encoding="utf-8"))
ROLES_JOB = ROLES["jobs"]["roles"]
CONFIRM = "BACKUP defaultdb TO ENCRYPTED ARTIFACT"
BACKUP, LEAK, UPLOAD = "Backup in one snapshot", "Leak check", "Upload ONLY"


def _step(prefix, steps=STEPS):
    return next(s for s in steps if s.get("name", "").startswith(prefix))


def _module():
    spec = importlib.util.spec_from_file_location("fcb_db_backup_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql_only(text):
    return re.sub(r"--[^\n]*", "", text)


# ------------------------------------------------------------------ triggers and gates

def test_manual_dispatch_only_with_the_confirmation_input():
    triggers = WF.get("on", WF.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"confirm"}
    assert triggers["workflow_dispatch"]["inputs"]["confirm"]["default"] == ""
    # Read-only token, same as every defaultdb workflow (actions: read for the environment gate).
    assert WF["permissions"] == {"contents": "read", "actions": "read"}
    assert "write" not in str(WF["permissions"])


def test_main_only_protected_environment_and_shared_concurrency():
    assert JOB["if"] == "github.ref == 'refs/heads/main'"
    assert JOB["environment"] == "aiven-production"
    restore = yaml.safe_load(RESTORE_PATH.read_text(encoding="utf-8"))
    assert WF["concurrency"] == ROLES["concurrency"] == restore["concurrency"]
    assert WF["concurrency"]["cancel-in-progress"] is False
    assert JOB["env"]["EXPECTED_DB"] == "defaultdb" and JOB["env"]["CONFIRM_BACKUP"] == CONFIRM


def test_the_g02_gates_are_the_first_steps_and_the_phrase_is_exact():
    assert STEPS[0]["uses"].startswith("actions/checkout@") and STEPS[0]["with"]["persist-credentials"] is False
    env_gate, dispatch_gate = STEPS[1]["run"], STEPS[2]["run"]
    assert 'reviewers="$(bash scripts/require_protected_environment.sh "$REPO" aiven-production)"' in env_gate
    assert dispatch_gate.splitlines()[1].strip() == "bash scripts/require_owner_main_dispatch.sh"
    assert 'if [ "$CONFIRM" != "$CONFIRM_BACKUP" ]' in dispatch_gate
    assert STEPS[2]["env"]["CONFIRM"] == "${{ inputs.confirm }}"
    for step in STEPS[:3]:
        assert not any(k in step for k in ("if", "continue-on-error"))
        assert "secrets." not in str(step.get("env", {}))
    # No step before the gates sees a secret, and the gates come before everything else.
    first_secret = next(i for i, s in enumerate(STEPS) if "secrets." in str(s.get("env", {})))
    assert first_secret == 3


def test_the_pinned_target_ca_tls_and_toolchain_mirror_the_roles_workflow():
    for key in ("EXPECTED_HOST_SHA256", "EXPECTED_PORT", "EXPECTED_PORT_SHA256", "EXPECTED_COUNTS",
                "EXPECTED_STRUCTURE_0008", "EXPECTED_DB", "FCB_DB_URL_VAR", "PGCONNECT_TIMEOUT"):
        assert JOB["env"][key] == ROLES_JOB["env"][key], key
    ca = "Materialize and authenticate the Aiven project CA"
    assert _step(ca)["run"] == _step(ca, ROLES_JOB["steps"])["run"]
    mask = "Mask connection details and enforce TLS"
    assert _step(mask)["run"] == _step(mask, ROLES_JOB["steps"])["run"]
    assert "python -I -S scripts/aiven_pg18_diag.py target" in _step(mask)["run"]
    install = _step("Install PostgreSQL 18")["run"]
    assert 'test "$fpr" = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"' in install
    assert "install -y -qq postgresql-client-18 postgresql-18 age" in install
    assert "create_main_cluster = false" in install
    pins = _step("Verify the pinned SHA-256")["run"]
    assert "pip install --quiet --require-hashes --no-deps -r scripts/fcb_db_roles_requirements.txt" in pins
    backup = _step(BACKUP)
    assert backup["env"]["PGSSLMODE"] == "verify-full"
    assert "default_transaction_read_only=on" in backup["env"]["PGOPTIONS"]
    assert "--require-tls" in backup["run"] and "python -I scripts/fcb_db_backup.py run" in backup["run"]


def test_every_sql_file_is_pinned_and_identical_to_the_roles_pins():
    pins = dict(line.split("=", 1) for line in JOB["env"]["EXPECTED_SQL_SHA256"].split())
    roles_pins = dict(line.split("=", 1) for line in ROLES_JOB["env"]["EXPECTED_SQL_SHA256"].split())
    assert set(pins) == {"sql/aiven/count_bridge_catalog.sql", "sql/aiven/fingerprint_bridge_structure.sql"}
    for path, sha in pins.items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == sha, path
        assert roles_pins[path] == sha
    assert "test \"$n\" -eq 2" in _step("Verify the pinned SHA-256")["run"]
    mod = _module()
    assert {str(mod.COUNT_SQL.relative_to(ROOT)), str(mod.STRUCTURE_SQL.relative_to(ROOT))} == set(pins)
    # The script reads no other SQL file.
    assert set(re.findall(r'AIVEN / "([^"]+)"', SCRIPT.read_text())) == {Path(p).name for p in pins}


# ------------------------------------------------------------------ least privilege

WRITE_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|GRANT|REVOKE|CREATE|COPY|COMMENT|"
                       r"SECURITY\s+LABEL|REINDEX|VACUUM|CLUSTER|LOCK|SET\s+ROLE|SET\s+SESSION|RESET|nextval|setval|"
                       r"set_config|pg_advisory|lo_)\b", re.I)


def test_source_side_sql_is_read_only_catalog_or_select():
    mod = _module()
    for name in ("GUARD_SQL", "TABLES_SQL", "SEQUENCES_SQL", "FUNCTIONS_SQL", "REFERENCED_ROLES_SQL", "OWNERS_SQL",
                 "ACLS_SQL", "ROLES_SQL", "MEMBERSHIPS_SQL", "BOOTSTRAP_SQL"):
        assert not WRITE_SQL.search(getattr(mod, name)), name
    for path in (mod.COUNT_SQL, mod.STRUCTURE_SQL):
        assert not WRITE_SQL.search(_sql_only(path.read_text())), path
    import inspect
    for fn in (mod._table_hash_sql, mod._sequence_sql, mod.read_evidence, mod.read_sequences, mod.read_globals,
               mod.read_acls):
        body = inspect.getsource(fn).split('"""')[-1] if fn.__doc__ else inspect.getsource(fn)
        assert not WRITE_SQL.search(" ".join(re.findall(r'"([^"]*)"', body))), fn.__name__


def test_the_source_section_opens_a_read_only_snapshot_and_runs_no_write():
    src = SCRIPT.read_text(encoding="utf-8")
    source = src[src.index("# ---- 1. source"):src.index("# ---- 2. no password")]
    assert 'conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")' in source
    assert "pg_export_snapshot()" in source and "--snapshot={snapshot}" in source
    assert source.index("GUARD_SQL") < source.index("read_sequences(conn)") < source.index('bin_("pg_dump")')
    assert "conn.rollback()" in source and "conn.commit()" not in source
    for literal in re.findall(r'conn\.execute\(("[^"]*")', source):
        assert not WRITE_SQL.search(literal), literal
    assert "--no-role-passwords" in source or "--no-role-passwords" in src
    mod = _module()
    assert "default_transaction_read_only=on" in mod.READ_GUCS and mod.DUMP_GUCS == "-c default_transaction_read_only=on"
    # Restore-only writes go to the isolated cluster connection (lc/rdb), never `conn`.
    assert "CREATE DATABASE" in src and "conn.execute(stmt)" not in src


def test_no_migration_apply_or_role_change_is_reachable_from_the_workflow_or_script():
    script = SCRIPT.read_text(encoding="utf-8")
    for text, label in ((WF_CODE, "workflow"), (script, "script")):
        for forbidden in ("sql/migrations", "aiven_migrations", "upsert_bridge_roles", "grant_bridge_runtime",
                          "enable_bridge_migrator", "roles_apply_guard", "finalize_bridge_privileges",
                          "prepare_bridge_roles", "oauth_throttle_0009_grant", "apifootball_cert_ledger_0010",
                          "SET ROLE", "--clean", "--if-exists", "mode=apply"):
            assert forbidden not in text, (label, forbidden)
    assert not re.search(r"\bpsql\b[^\n]*FCB_DEFAULTDB_URL", WF_CODE)  # the workflow runs no SQL itself
    assert not re.search(r"\s-1\s", WF_CODE) and "--single-transaction" not in WF_CODE
    referenced = set(re.findall(r"sql/aiven/[A-Za-z0-9_]+\.sql", WF_TEXT))
    assert referenced == {"sql/aiven/count_bridge_catalog.sql", "sql/aiven/fingerprint_bridge_structure.sql"}
    assert set(re.findall(r"scripts/[A-Za-z0-9_]+\.(?:py|sh|txt)", WF_TEXT)) == {
        "scripts/require_protected_environment.sh", "scripts/require_owner_main_dispatch.sh",
        "scripts/fcb_db_backup.py", "scripts/fcb_db_roles_requirements.txt", "scripts/aiven_pg18_diag.py"}
    assert not re.search(r"\bsxbet|signer\b", WF_CODE, re.I)


# ------------------------------------------------------------------ secrets

def test_secrets_reach_only_the_steps_that_need_them_and_are_never_echoed():
    allowed = {
        "FCB_BACKUP_PASSPHRASE": {"Fail closed if a protected input is missing", BACKUP, LEAK},
        "FCB_DEFAULTDB_URL": {"Fail closed if a protected input is missing", "Mask connection details", BACKUP, LEAK},
        "FCB_AIVEN_CA_PEM": {"Fail closed if a protected input is missing", "Materialize and authenticate"},
        "FCB_AIVEN_CA_SHA256": {"Fail closed if a protected input is missing", "Materialize and authenticate"},
    }
    seen = set(re.findall(r"secrets\.([A-Z0-9_]+)", WF_TEXT))
    assert seen == set(allowed)
    for step in STEPS:
        env = step.get("env") or {}
        run = str(step.get("run", ""))
        assert "secrets." not in run and "secrets." not in str(step.get("with", {})), step.get("name")
        for name, value in env.items():
            if "secrets." in str(value):
                assert value == f"${{{{ secrets.{name} }}}}", name
                assert any(step["name"].startswith(p) for p in allowed[name]), (name, step["name"])
        # Never printed or passed on a command line (the CA certificate is public and written to a file).
        for var in ("FCB_BACKUP_PASSPHRASE", "FCB_DEFAULTDB_URL"):
            assert not re.search(rf"(echo|printf|tee|cat)[^\n]*\${{?{var}", run), (step.get("name"), var)
            assert not re.search(rf"--[a-z-]+[ =]\"?\${{?{var}", run), (step.get("name"), var)
    for step in STEPS:
        assert "set -x" not in str(step.get("run", ""))
    assert "FCB_BACKUP_PASSPHRASE" not in _step(BACKUP)["run"]


def test_upload_contains_only_the_ciphertext_and_its_sha256_for_one_day():
    up = _step(UPLOAD)
    assert up["uses"] == "actions/upload-artifact@v4"
    w = up["with"]
    assert w["retention-days"] == 1 and w["if-no-files-found"] == "error"
    assert "${{ github.run_id }}" in w["name"]
    paths = [p.strip() for p in w["path"].strip().splitlines()]
    base = "${{ runner.temp }}/fcb-backup-out/fcb-defaultdb-${{ github.run_id }}-${{ github.run_attempt }}"
    assert paths == [f"{base}.tar.age", f"{base}.tar.age.sha256"]
    assert not any(k in up for k in ("if", "continue-on-error"))  # only after every step succeeded
    names = [s.get("name", "") for s in STEPS]
    assert names.index(_step(BACKUP)["name"]) < names.index(_step(LEAK)["name"]) < names.index(up["name"])
    assert "--outdir \"$RUNNER_TEMP/fcb-backup-out\"" in _step(BACKUP)["run"]
    assert "--workdir \"$RUNNER_TEMP/fcb-backup-work\"" in _step(BACKUP)["run"]
    assert "fcb-backup-work" not in w["path"] and "summary" not in w["path"]
    assert sum(1 for s in STEPS if "upload-artifact" in str(s.get("uses", ""))) == 1
    cleanup = STEPS[-1]
    assert cleanup["if"] == "${{ always() }}" and "fcb-backup-out" in cleanup["run"] and "fcb-backup-work" in cleanup["run"]


def test_summary_is_published_only_after_the_leak_check():
    run = _step(BACKUP)["run"]
    assert '--summary "$RUNNER_TEMP/fcb-backup-summary.md" --publish-to "$GITHUB_STEP_SUMMARY"' in run
    leak = _step(LEAK)
    assert "if" not in leak  # runs only when every gate and the backup succeeded
    assert '--artifact-dir "$RUNNER_TEMP/fcb-backup-out"' in leak["run"] and "GITHUB_STEP_SUMMARY" not in leak["run"]


def test_publish_summary_refuses_a_leaking_summary(tmp_path):
    mod = _module()
    private, public = tmp_path / "p.md", tmp_path / "pub.md"
    private.write_text("| restore_owners | PASS | objects=40 |\n")
    assert mod.publish_summary(str(private), str(public), ENV) and public.read_text() == private.read_text()
    private.write_text("oops " + ENV["FCB_BACKUP_PASSPHRASE"])
    assert not mod.publish_summary(str(private), str(public), ENV)
    assert ENV["FCB_BACKUP_PASSPHRASE"] not in public.read_text()


# ------------------------------------------------------------------ script units

@pytest.mark.parametrize("value, category", [
    (None, "PASSPHRASE_MISSING"), ("", "PASSPHRASE_MISSING"), ("a" * 31, "PASSPHRASE_TOO_SHORT"),
    ("a" * 1025, "PASSPHRASE_TOO_LONG"), ("a" * 40 + "\n", "PASSPHRASE_CONTROL_CHARACTER"),
    (" " + "a" * 40, "PASSPHRASE_SURROUNDING_WHITESPACE"), ("a" * 32, None), ("correct horse " * 4, None),
])
def test_passphrase_policy_fails_closed(value, category):
    env = {} if value is None else {"FCB_BACKUP_PASSPHRASE": value}
    if value == "correct horse " * 4:
        env["FCB_BACKUP_PASSPHRASE"] = value.strip()
    assert _module().passphrase_problem(env) == category


def test_check_passphrase_cli_prints_only_a_category(capsys, monkeypatch):
    mod = _module()
    secret = "S" * 20
    monkeypatch.setenv("FCB_BACKUP_PASSPHRASE", secret)
    assert mod.main(["x", "check-passphrase"]) == 1
    out = capsys.readouterr()
    assert "PASSPHRASE_TOO_SHORT" in out.err and secret not in out.out + out.err


@pytest.mark.parametrize("url, code", [
    ("", "SOURCE_URL_MISSING"),
    ("mysql://u:p@h:1/defaultdb?sslmode=verify-full", "SOURCE_URL_SCHEME"),
    ("postgresql://u:p@h1,h2:1/defaultdb?sslmode=verify-full", "SOURCE_URL_HOST"),
    ("postgresql://u:p@h:1/other?sslmode=verify-full", "SOURCE_URL_DATABASE"),
    ("postgresql://u:p@h:1/defaultdb?sslmode=verify-full&host=evil", "SOURCE_URL_QUERY"),
    ("postgresql://u:p@h:1/defaultdb?sslmode=require", "SOURCE_SSLMODE_NOT_VERIFY_FULL"),
    ("postgresql://u:p@h:1/defaultdb", "SOURCE_SSLMODE_NOT_VERIFY_FULL"),
])
def test_source_url_is_refused_with_a_code_only(url, code):
    mod = _module()
    with pytest.raises(mod.BackupError) as exc:
        mod.source_params({"FCB_DEFAULTDB_URL": url, "PGSSLMODE": "verify-full", "PGSSLROOTCERT": "/ca"},
                          expected_db="defaultdb", require_tls=True)
    assert exc.value.code == code and "p@" not in str(exc.value)


def test_source_params_need_pgsslmode_and_the_ca_and_go_to_children_only_as_libpq_env():
    mod = _module()
    url = "postgresql://adm:pw%40x@db.example:14274/defaultdb?sslmode=verify-full"
    with pytest.raises(mod.BackupError, match="SOURCE_PGSSLMODE_NOT_VERIFY_FULL"):
        mod.source_params({"FCB_DEFAULTDB_URL": url}, expected_db="defaultdb", require_tls=True)
    with pytest.raises(mod.BackupError, match="SOURCE_CA_MISSING"):
        mod.source_params({"FCB_DEFAULTDB_URL": url, "PGSSLMODE": "verify-full"}, expected_db="defaultdb", require_tls=True)
    p = mod.source_params({"FCB_DEFAULTDB_URL": url, "PGSSLMODE": "verify-full", "PGSSLROOTCERT": "/ca.pem"},
                          expected_db="defaultdb", require_tls=True)
    assert p == {"host": "db.example", "port": "14274", "user": "adm", "password": "pw@x", "dbname": "defaultdb",
                 "sslmode": "verify-full", "sslrootcert": "/ca.pem"}


def test_child_environment_carries_no_secret(monkeypatch):
    mod = _module()
    monkeypatch.setenv("FCB_BACKUP_PASSPHRASE", "P" * 40)
    monkeypatch.setenv("FCB_DEFAULTDB_URL", "postgresql://u:secretpw@h:1/defaultdb")
    monkeypatch.setenv("PGPASSWORD", "inherited")
    env = mod.child_env({"host": "h", "password": "pw"}, options=mod.DUMP_GUCS)
    assert not any(k.startswith("FCB_") for k in env)
    assert "P" * 40 not in str(env) and "secretpw" not in str(env) and "inherited" not in str(env)
    assert env["PGPASSWORD"] == "pw" and env["PGOPTIONS"] == "-c default_transaction_read_only=on"
    assert set(mod.child_env()) == {"PATH", "LANG", "LC_ALL", "HOME", "PGAPPNAME", "PGCONNECT_TIMEOUT", "TZ"}


def test_age_receives_the_passphrase_only_through_its_terminal(tmp_path):
    """A stand-in `age` records argv and environment and reads both prompts from /dev/tty."""
    mod = _module()
    rec = tmp_path / "rec"
    fake = tmp_path / "age"
    fake.write_text(
        "#!/bin/sh\n"
        f"{{ printf 'ARGV %s\\n' \"$@\"; env; }} > '{rec}'\n"
        "printf 'Enter passphrase (leave empty to autogenerate a secure one): ' > /dev/tty\n"
        "IFS= read -r a < /dev/tty\n"
        "printf 'Confirm passphrase: ' > /dev/tty\n"
        "IFS= read -r b < /dev/tty\n"
        f"[ \"$a\" = \"$b\" ] && printf '%s' \"$a\" | sha256sum | cut -d' ' -f1 > '{tmp_path}/got'\n")
    fake.chmod(0o700)
    secret = "Pass-" + "x" * 40
    rc = mod._age([str(fake), "-p", "-o", str(tmp_path / "o"), str(tmp_path / "i")], secret, prompts=2, timeout=30)
    assert rc == 0
    assert (tmp_path / "got").read_text().strip() == hashlib.sha256(secret.encode()).hexdigest()
    recorded = rec.read_text()
    assert secret not in recorded and "FCB_" not in recorded and "PGPASSWORD" not in recorded
    assert set(l.split("=", 1)[0] for l in recorded.splitlines() if "=" in l and not l.startswith("ARGV")) <= {
        "PATH", "LANG", "PWD", "SHLVL", "_", "OLDPWD"}


def test_password_material_detection():
    mod = _module()
    assert mod.password_findings("CREATE ROLE a;\nALTER ROLE a WITH NOSUPERUSER LOGIN;\n") == []
    assert mod.password_findings("ALTER ROLE a WITH LOGIN PASSWORD 'x';") == ["PASSWORD_CLAUSE"]
    assert "SCRAM_VERIFIER" in mod.password_findings("'SCRAM-SHA-256$4096:abc$def:ghi'")
    assert mod.password_findings("x md5" + "0" * 32 + " y") == ["MD5_HASH"]


def _guard_row(**over):
    row = {"db": "defaultdb", "ver": 180006, "ssl": True, "superuser": False, "same_user": True, "read_only": "on",
           "isolation": "repeatable read", "other_schemas": None, "own_other_schemas": None, "foreign_owned": 0,
           "acting_members": 0, "large_objects": 0, "forced_rls": 0}
    row.update(over)
    return row


@pytest.mark.parametrize("over, problem", [
    ({"db": "x"}, "WRONG_DATABASE"), ({"ver": 170004}, "SERVER_NOT_POSTGRESQL_18"), ({"ssl": False}, "SOURCE_NOT_TLS"),
    ({"same_user": False}, "CURRENT_USER_IS_NOT_SESSION_USER"), ({"read_only": "off"}, "TRANSACTION_NOT_READ_ONLY"),
    ({"isolation": "read committed"}, "TRANSACTION_NOT_SNAPSHOT"), ({"other_schemas": ["extra"]}, "UNEXPECTED_SCHEMA"),
    ({"other_schemas": ["_avnadmin_monitor"], "own_other_schemas": ["_avnadmin_monitor"]}, "UNEXPECTED_SCHEMA"),
    ({"foreign_owned": 1}, "PUBLIC_OBJECT_NOT_OWNED_BY_SESSION_USER"),
    ({"acting_members": 1}, "ANOTHER_ROLE_CAN_ACT_AS_SESSION_USER"), ({"large_objects": 2}, "LARGE_OBJECTS_PRESENT"),
    ({"forced_rls": 1}, "FORCED_ROW_LEVEL_SECURITY"),
])
def test_source_guard_refuses(over, problem):
    problems, _ = _module().check_guard(_guard_row(**over), expected_db="defaultdb", require_tls=True)
    assert problem in problems


def test_source_guard_admits_the_aiven_monitor_schema_by_excluding_it():
    problems, excluded = _module().check_guard(_guard_row(other_schemas=["_avnadmin_monitor"]),
                                               expected_db="defaultdb", require_tls=True)
    assert problems == [] and excluded == ["_avnadmin_monitor"]


def _artifact(tmp_path, payload=b"\x00" * 64):
    out = tmp_path / "out"
    out.mkdir()
    data = b"age-encryption.org/v1\n-> scrypt c2FsdA 18\nd3JhcA\n--- bWFj\n" + payload
    (out / "fcb-defaultdb-1-1.tar.age").write_bytes(data)
    (out / "fcb-defaultdb-1-1.tar.age.sha256").write_text(f"{hashlib.sha256(data).hexdigest()}  fcb-defaultdb-1-1.tar.age\n")
    summary = tmp_path / "s.md"
    summary.write_text("## FCB defaultdb encrypted backup: PASS\n| restore_owners | PASS | objects=40 |\n")
    return out, summary


ENV = {"FCB_DEFAULTDB_URL": "postgresql://avnadmin:Sup3rSecretPw@pg-host.aivencloud.com:14274/defaultdb?sslmode=verify-full",
       "FCB_BACKUP_PASSPHRASE": "Q" * 48}


def test_leak_check_passes_on_a_clean_artifact_and_summary(tmp_path):
    out, summary = _artifact(tmp_path)
    assert _module().leak_problems(summary, out, ENV) == []
    assert _module().leak_problems(summary, None, ENV) == []


@pytest.mark.parametrize("mutate, problem", [
    (lambda out, s: (out / "defaultdb.dump").write_bytes(b"PGDMP"), "ARTIFACT_DIR_NOT_EXACTLY_CIPHERTEXT_AND_SHA256"),
    (lambda out, s: (out / "roles.sql").write_text("x"), "ARTIFACT_DIR_NOT_EXACTLY_CIPHERTEXT_AND_SHA256"),
    (lambda out, s: (out / "fcb-defaultdb-1-1.tar.age.sha256").unlink(), "ARTIFACT_DIR_NOT_EXACTLY_CIPHERTEXT_AND_SHA256"),
    (lambda out, s: (out / "fcb-defaultdb-1-1.tar.age").write_bytes(b"PGDMP plaintext"), "ARTIFACT_NOT_AGE"),
    (lambda out, s: (out / "fcb-defaultdb-1-1.tar.age").write_bytes(b"age-encryption.org/v1\n--- x\nustar"),
     "ARTIFACT_CONTAINS_PLAINTEXT_OR_SECRET"),
    (lambda out, s: s.write_text("pass " + "Q" * 48), "SUMMARY_CONTAINS_SECRET"),
    (lambda out, s: s.write_text("host pg-host.aivencloud.com"), "SUMMARY_CONTAINS_SECRET"),
    (lambda out, s: s.write_text("pw Sup3rSecretPw"), "SUMMARY_CONTAINS_SECRET"),
    (lambda out, s: s.write_text("postgresql://x"), "SUMMARY_CONTAINS_FORBIDDEN_TEXT"),
    (lambda out, s: s.write_text("SCRAM-SHA-256$4096:a"), "SUMMARY_CONTAINS_FORBIDDEN_TEXT"),
])
def test_leak_check_fails(tmp_path, mutate, problem):
    out, summary = _artifact(tmp_path)
    mutate(out, summary)
    problems = _module().leak_problems(summary, out, ENV)
    assert problem in problems, problems


def test_leak_check_detects_a_sha256_mismatch_and_the_passphrase_in_the_ciphertext(tmp_path):
    out, summary = _artifact(tmp_path, payload=b"Q" * 48)
    assert "ARTIFACT_CONTAINS_PLAINTEXT_OR_SECRET" in _module().leak_problems(summary, out, ENV)
    (out / "fcb-defaultdb-1-1.tar.age.sha256").write_text("0" * 64 + "  fcb-defaultdb-1-1.tar.age\n")
    assert "ARTIFACT_SHA256_MISMATCH" in _module().leak_problems(summary, out, ENV)


def test_sanitize_redacts_the_passphrase_and_uri_parts(tmp_path, monkeypatch):
    mod = _module()
    monkeypatch.setenv("FCB_DEFAULTDB_URL", ENV["FCB_DEFAULTDB_URL"])
    monkeypatch.setenv("FCB_BACKUP_PASSPHRASE", ENV["FCB_BACKUP_PASSPHRASE"])
    text = ('pg_dump: error: connection to server at "pg-host.aivencloud.com" port 14274 failed: '
            f'FATAL: password authentication failed for user "avnadmin" {"Q" * 48}\nLINE 1: ALTER ROLE x PASSWORD \'y\'\n')
    out = mod.sanitize(text)
    assert "pg-host" not in out and "Q" * 48 not in out and "avnadmin" not in out and "ALTER ROLE" not in out


def test_run_id_and_artifact_names():
    mod = _module()
    assert mod.main(["x", "run", "--workdir", "/nonexistent/w", "--outdir", "/nonexistent/o", "--expected-counts", "a",
                     "--expected-structure", "b", "--source-commit", "c", "--run-id", "1;rm -rf /"]) == 1


def test_runbook_covers_the_owner_procedure():
    text = RUNBOOK.read_text(encoding="utf-8")
    for needle in ("FCB_BACKUP_PASSPHRASE", "32", "password manager", CONFIRM, "FCB-WP009-Backups", "sha256sum",
                   "age -d", "pg_restore --exit-on-error", "--no-role-passwords", "DOCS_UNVERIFIED",
                   "_avnadmin_monitor", "retention", "elimin"):
        assert needle in text, needle
    assert "artifact" in text.lower() and "Drive" in text
