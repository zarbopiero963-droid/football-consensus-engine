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
import json
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
                "EXPECTED_STRUCTURE_0008", "EXPECTED_DB", "FCB_DB_URL_VAR", "PGCONNECT_TIMEOUT", "PGPORT"):
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
    # PATH A2 R1: GUARD_SQL embeds the canonical fcb_cert_quota state query, whose string literals
    # ('/CREATE/false', 'CREATE', 'USAGE') are compared data; with its literals blanked it writes nothing,
    # and the rest of GUARD_SQL is checked unchanged.
    state = mod.CERT_QUOTA_STATE_SQL
    assert mod.GUARD_SQL.count(state) == 1
    assert not WRITE_SQL.search(re.sub(r"'(?:[^']|'')*'", "''", state))
    assert not re.search(r"\b(EXECUTE|DO|PERFORM)\b", re.sub(r"'(?:[^']|'')*'", "''", state), re.I)
    for name in ("GUARD_SQL", "TABLES_SQL", "SEQUENCES_SQL", "FUNCTIONS_SQL", "REFERENCED_ROLES_SQL", "OWNERS_SQL",
                 "ACLS_SQL", "ROLES_SQL", "MEMBERSHIPS_SQL", "BOOTSTRAP_SQL", "COLUMN_OPTIONS_SQL", "DATABASE_SQL"):
        assert not WRITE_SQL.search(getattr(mod, name).replace(state, "")), name
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
    assert 'conn.execute("ROLLBACK")' in source and "COMMIT" not in source and ".commit()" not in source
    # Explicit transaction: autocommit connection, then the BEGIN ... READ ONLY (no implicit BEGIN).
    assert "autocommit=True" in source and "autocommit=False" not in source
    assert 'for guc in ("statement_timeout", "idle_in_transaction_session_timeout", "transaction_timeout"):' in source
    assert 'conn.execute(f"SET LOCAL {guc} = 0")' in source
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
    assert cleanup["run"].index("pg_ctl") < cleanup["run"].index("rm -rf")  # cluster stopped before its data is removed


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


ENV = {"FCB_DEFAULTDB_URL": "postgresql://avnadmin:fake-test-pw-not-real@pg-host.aivencloud.com:14274/defaultdb?sslmode=verify-full",
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
    (lambda out, s: s.write_text("pw fake-test-pw-not-real"), "SUMMARY_CONTAINS_SECRET"),
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


# ------------------------------------------------------------------ review #202 regressions

AIVEN_ENV = {"FCB_DEFAULTDB_URL": "postgresql://avnadmin:fake-test-pw-not-real@pg-host.aivencloud.com:14274/defaultdb?sslmode=verify-full",
             "FCB_BACKUP_PASSPHRASE": "Q" * 48}


def test_p1_the_aiven_monitor_schema_name_is_not_a_leak_of_the_avnadmin_user(tmp_path):
    """RED on f56fcf8: `avnadmin` (the production user) is a substring of `_avnadmin_monitor`."""
    out, summary = _artifact(tmp_path)
    summary.write_text("## FCB defaultdb encrypted backup: PASS\n- excluded_schemas: `_avnadmin_monitor`\n"
                       "| source_guard_catalog_only | PASS | ok |\n")
    assert _module().leak_problems(summary, out, AIVEN_ENV) == []


@pytest.mark.parametrize("text", ["user avnadmin connected", "avnadmin", "role=avnadmin;", "fake-test-pw-not-real",
                                  "pg-host.aivencloud.com", "Q" * 48])
def test_p1_real_secrets_and_the_bare_username_are_still_leaks(tmp_path, text):
    out, summary = _artifact(tmp_path)
    summary.write_text(text + "\n")
    assert "SUMMARY_CONTAINS_SECRET" in _module().leak_problems(summary, out, AIVEN_ENV)


FAKE_ROW = "tok_9f8e7d6c5b4a_SECRET_ROW_VALUE"
DATA_STDERR = (
    'pg_restore: error: could not execute query: ERROR:  duplicate key value violates unique constraint "oauth_access_tokens_pkey"\n'
    f"DETAIL:  Key (token_hash)=({FAKE_ROW}) already exists.\n"
    f"CONTEXT:  COPY oauth_access_tokens, line 1: \"{FAKE_ROW}\tclient\t2030-01-01\"\n"
    f"Command was: COPY public.oauth_access_tokens (token_hash) FROM stdin; {FAKE_ROW}\n"
    f"psql:roles.sql:12: ERROR:  invalid value for parameter \"search_path\": \"{FAKE_ROW}\"\n")


def _failing_tool(tmp_path):
    tool = tmp_path / "pg_restore"
    (tmp_path / "stderr.txt").write_text(DATA_STDERR)
    tool.write_text(f"#!/bin/sh\ncat '{tmp_path / 'stderr.txt'}' >&2\nexit 1\n")
    tool.chmod(0o700)
    return tool


def test_p2_a_failing_client_never_surfaces_detail_context_or_row_values(tmp_path, capsys):
    """RED on f56fcf8: the DETAIL/CONTEXT/Command was lines reached the job log via exc.detail."""
    mod = _module()
    with pytest.raises(mod.BackupError) as exc:
        mod._run([str(_failing_tool(tmp_path))], mod.child_env(), "PG_RESTORE_FAILED")
    shown = f"{exc.value} {exc.value.code} {exc.value.detail}"
    assert FAKE_ROW not in shown and "DETAIL" not in shown and "CONTEXT" not in shown and "COPY" not in shown
    assert exc.value.code == "PG_RESTORE_FAILED" and exc.value.detail == "class=UNIQUE_VIOLATION"


@pytest.mark.parametrize("stderr, cls", [
    ("ERROR:  permission denied for table x\nDETAIL: secret", "PERMISSION_DENIED"),
    ('ERROR:  relation "x" already exists', "ALREADY_EXISTS"),
    ('ERROR:  role "x" does not exist', "DOES_NOT_EXIST"),
    ("ERROR:  new row for relation \"t\" violates check constraint \"c\"\nDETAIL: Failing row contains (secret).", "CONSTRAINT_VIOLATION"),
    ("connection to server at \"h\" (1.2.3.4), port 5 failed: FATAL:  password authentication failed for user \"u\"", "AUTHENTICATION_FAILED"),
    ("connection to server at \"h\", port 5 failed: Connection refused", "CONNECTION_FAILED"),
    ("ERROR:  canceling statement due to lock timeout", "TIMEOUT"),
    ("ERROR:  syntax error at or near \"secret\"", "SYNTAX_ERROR"),
    ("ERROR:  something nobody anticipated: secret", "OTHER"),
    ("", "NO_MESSAGE"),
])
def test_p2_errors_are_reported_as_fixed_classes_only(stderr, cls):
    assert _module().error_class(stderr) == cls


def test_p2_cli_error_lines_print_codes_only(tmp_path, capsys, monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod, "passphrase_problem", lambda env=None: None)
    monkeypatch.setattr(mod, "age_version", lambda: "1.1.1")
    monkeypatch.setattr(mod, "tool_major", lambda b, t: (_ for _ in ()).throw(mod.BackupError("PG_DUMP_FAILED", "class=OTHER")))
    args = mod._parser().parse_args(["run", "--workdir", str(tmp_path / "w"), "--outdir", str(tmp_path / "o"),
                                     "--expected-counts", "x", "--expected-structure", "y", "--source-commit", "c",
                                     "--run-id", "1", "--summary", str(tmp_path / "s.md")])
    assert mod.run(args, env={"FCB_BACKUP_PASSPHRASE": "Q" * 48}) == 1
    err = capsys.readouterr().err
    assert err.strip() == "::error::PG_DUMP_FAILED class=OTHER"


def test_p3_snapshot_session_zeroes_every_timeout_at_startup_too():
    gucs = _module().READ_GUCS
    for g in ("statement_timeout=0", "idle_in_transaction_session_timeout=0", "transaction_timeout=0",
              "default_transaction_read_only=on", "row_security=off"):
        assert f"-c {g}" in gucs, g


def test_p3_extensions_outside_public_are_refused_deterministically():
    problems, _ = _module().check_guard(_guard_row(extensions_outside_public=1, other_schemas=["_avnadmin_monitor"]),
                                        expected_db="defaultdb", require_tls=True)
    assert problems == ["EXTENSION_OUTSIDE_PUBLIC"]
    assert "pg_catalog.pg_extension" in _module().GUARD_SQL


DB_SQL = """--
-- PostgreSQL database dump
--
\\restrict abc
SET statement_timeout = 0;
SELECT pg_catalog.set_config('search_path', '', false);
CREATE DATABASE wp009_x WITH TEMPLATE = template0 ENCODING = 'UTF8' LOCALE_PROVIDER = libc LOCALE = 'C.UTF-8';
ALTER DATABASE wp009_x OWNER TO adm;
\\unrestrict abc
\\connect wp009_x
\\restrict abc
ALTER DATABASE wp009_x SET work_mem TO '8MB';
ALTER ROLE adm IN DATABASE wp009_x SET statement_timeout TO '5min';
REVOKE TEMPORARY ON DATABASE wp009_x FROM PUBLIC;
GRANT CONNECT ON DATABASE wp009_x TO fcb_app;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA _avnadmin_monitor;
COMMENT ON EXTENSION pgcrypto IS 'x';
\\unrestrict abc
"""


def test_p3_database_sql_replay_is_retargeted_and_complete():
    out = _module().database_replay_sql(DB_SQL, "wp009_x", "defaultdb")
    assert out.splitlines() == [
        "ALTER DATABASE defaultdb OWNER TO adm;",
        "ALTER DATABASE defaultdb SET work_mem TO '8MB';",
        "ALTER ROLE adm IN DATABASE defaultdb SET statement_timeout TO '5min';",
        "REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC;",
        "GRANT CONNECT ON DATABASE defaultdb TO fcb_app;"]
    assert "EXTENSION" not in out and "CREATE DATABASE" not in out


@pytest.mark.parametrize("line", ["DROP TABLE x;", "ALTER DATABASE other SET a TO 'b';", "CREATE ROLE x;",
                                  "GRANT SELECT ON TABLE t TO x;", "ALTER DATABASE wp009_x SET a TO 'multi"])
def test_p3_database_sql_replay_refuses_anything_unexpected(line):
    mod = _module()
    with pytest.raises(mod.BackupError, match="DATABASE_SQL_UNEXPECTED_STATEMENT"):
        mod.database_replay_sql(DB_SQL + line + "\n", "wp009_x", "defaultdb")


def test_p3_public_summary_has_no_per_table_counts_and_no_schema_names():
    mod = _module()
    checks = mod.Checks()
    src = {"catalog_counts": "c", "structure": "s", "migrations": ["1"], "functions": [], "functions_sha256": "f",
           "sequences": {}, "owners": [], "owners_sha256": "o", "acls": [], "acls_sha256": "a",
           "tables": {"oauth_access_tokens": {"rows": 1234, "sha256": "h1"}, "t2": {"rows": 7, "sha256": "h2"}},
           "content_sha256": "x"}
    dst = {**src, "tables": {"oauth_access_tokens": {"rows": 1234, "sha256": "h1"}, "t2": {"rows": 6, "sha256": "h3"}}}
    mod._compare(checks, src, dst, "c", "s", ["1"])
    text = " ".join(f"{n} {i}" for n, _, i in checks.items)
    assert "1234" not in text and "rows oauth_access_tokens" not in text
    assert "rows_total=1240" in text and "mismatched=t2" in text
    names = [n for n, ok, _ in checks.items if not ok]
    assert names == ["restore_rows_per_table", "restore_content_per_table", "restore_content_sha256"] or \
        set(names) >= {"restore_rows_per_table", "restore_content_per_table"}


def test_column_options_ignore_only_a_well_formed_pg_attver():
    sql_text = _module().COLUMN_OPTIONS_SQL
    assert "o.opt OPERATOR(pg_catalog.!~) '^pg_attver=[0-9]+$'::pg_catalog.text" in sql_text
    assert sql_text.count("pg_attver") == 1


# Review #203 F1 (owner contract FCB-WP009-PR203-F1-PORT-RESOLUTION): the secret may reach the job
# with no port or an empty one ("host:"). libpq would use PGPORT, but explicit params override it,
# so the script resolves the port from libpq's own parse and never falls back to 5432.
_PIN = "14274"
_PORT_ENV = {"PGSSLMODE": "verify-full", "PGSSLROOTCERT": "/ca", "EXPECTED_PORT": _PIN, "PGPORT": _PIN}


def _params(netloc, mod=None, **env_patch):
    mod = mod or _module()
    env = {**_PORT_ENV, **env_patch, "FCB_DEFAULTDB_URL": f"postgresql://adm:pw@{netloc}/defaultdb?sslmode=verify-full"}
    return mod, mod.source_params({k: v for k, v in env.items() if v is not None},
                                  expected_db="defaultdb", require_tls=True)


@pytest.mark.parametrize("netloc,pgport", [
    ("db.example:14274", _PIN),   # :14274 + PGPORT 14274 -> accept
    ("db.example:14274", None),   # explicit pinned port, PGPORT unset -> accept
    ("db.example", _PIN),         # absent -> PGPORT
    ("db.example:", _PIN),        # empty "host:" -> PGPORT
])
def test_source_port_contract_accepts_only_the_pinned_port(netloc, pgport):
    mod, p = _params(netloc, PGPORT=pgport)
    assert p["port"] == _PIN
    assert mod.child_env(p)["PGPORT"] == _PIN   # pg_dump / pg_dumpall get the same port


@pytest.mark.parametrize("netloc,pgport", [
    ("db.example:5432", _PIN),     # explicit port other than the pinned one, whatever PGPORT says
    ("db.example:15000", "15000"),
    ("db.example:014274", _PIN),   # Python reads 14274, libpq passes "014274": no divergence allowed
    ("db.example", None),          # absent + PGPORT absent
    ("db.example:", None),         # empty + PGPORT absent
    ("db.example", "5432"),        # absent + PGPORT different
    ("db.example:", "5432"),       # empty + PGPORT different
    ("db.example", ""),            # absent + PGPORT empty
])
def test_source_port_contract_refuses_everything_else(netloc, pgport):
    mod = _module()
    with pytest.raises(mod.BackupError) as exc:
        _params(netloc, mod, PGPORT=pgport)
    assert exc.value.code in ("SOURCE_URL_PORT", "SOURCE_URL_UNPARSEABLE") and "pw" not in str(exc.value)
    if netloc != "db.example:014274":
        assert exc.value.code == "SOURCE_URL_PORT"


@pytest.mark.parametrize("netloc", ["db.example:14274", "db.example", "db.example:"])
def test_resolved_port_matches_libpq_and_never_5432(netloc):
    """The resolved port equals what libpq itself would use: its parsed port, else PGPORT."""
    from psycopg.conninfo import conninfo_to_dict
    _, p = _params(netloc)
    libpq = conninfo_to_dict(f"postgresql://adm:pw@{netloc}/defaultdb?sslmode=verify-full").get("port") or _PIN
    assert p["port"] == libpq == _PIN != "5432"


def test_the_real_postgresql_client_connects_to_the_resolved_port(monkeypatch):
    """psycopg/libpq, given the resolved params, opens its TCP connection on exactly that port:
    a local listener on the pinned port receives it (TLS then fails against the dummy, by design)."""
    import socket
    import threading
    import psycopg
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = str(srv.getsockname()[1])
    hits = []

    def accept():
        srv.settimeout(5)
        try:
            c, _ = srv.accept()
            hits.append(True)
            c.close()
        except OSError:
            pass
    t = threading.Thread(target=accept)
    t.start()
    mod = _module()
    p = mod.source_params({"FCB_DEFAULTDB_URL": "postgresql://adm:pw@127.0.0.1:/defaultdb?sslmode=require",
                           "EXPECTED_PORT": port, "PGPORT": port}, expected_db="defaultdb", require_tls=False)
    assert p["port"] == port
    for k in ("PGPORT", "PGHOST", "PGSERVICE"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(**p, connect_timeout=3)
    t.join(6)
    srv.close()
    assert hits, "libpq did not connect to the resolved port"


# ------------------------------------------------------------------ FCB-WP009-AIVEN-MANAGED-ROLES-RESTORE-FIX
# Run 43870519736 (main 5e6bc1d) failed with ROLES_SQL_APPLY_FAILED class=INVALID_SETTING: Aiven's
# roles dump sets avn_wal_sender_stop_timeout (unknown to stock PostgreSQL) on its service roles.
# The fixture is real `pg_dumpall --roles-only --no-role-passwords` output of PostgreSQL 18.6 for a
# local cluster holding the observed Aiven roles, memberships and settings (placeholder values; the
# dotted names are SET-able placeholders), with the three avn_wal_sender_stop_timeout lines injected
# because no stock server accepts them.

AIVEN_ROLES_FIXTURE = ROOT / "tests" / "fixtures" / "wp009_backup" / "aiven_roles_pg18.sql"
SERVICE_PARAMS = {"avn_wal_sender_stop_timeout", "default_transaction_isolation", "default_transaction_read_only",
                  "pg_stat_monitor.pgsm_track", "pg_stat_plans.track", "pg_stat_statements.track", "pgaudit.log",
                  "timescaledb.disable_load"}
MONITOR_PARAMS = {"extwlist.extensions", "pg_stat_monitor.pgsm_track", "pg_stat_plans.track",
                  "pg_stat_statements.track", "pgaudit.log"}


def test_aiven_managed_allowlist_is_exact_names_and_observed_params_only():
    mod = _module()
    assert {r: set(p) for r, p in mod.AIVEN_MANAGED_ROLE_SETTINGS.items()} == {
        "_aiven": SERVICE_PARAMS, "_aiven_repl": SERVICE_PARAMS, "postgres": SERVICE_PARAMS,
        "_avnadmin_monitor": MONITOR_PARAMS}
    assert mod.AIVEN_MANAGED_ROLES == {"_aiven", "_aiven_repl", "_aiven_rewind", "_avnadmin_auditing",
                                       "_avnadmin_managed", "_avnadmin_monitor", "postgres"}
    # The application roles are never managed, whatever their settings.
    assert not {"avnadmin", "avnadmin_group", "fcb_app", "fcb_migrator"} & set(mod.AIVEN_MANAGED_ROLE_SETTINGS)
    assert len(mod.managed_setting_pairs()) == 3 * 8 + 5
    # No prefix logic anywhere in the decision.
    src = SCRIPT.read_text(encoding="utf-8")
    body = src[src.index("def managed_setting_of"):src.index("def restore_test_roles_sql")]
    assert "startswith" not in body and "_aiven" not in body


def test_red_the_real_aiven_roles_dump_carries_a_setting_stock_postgresql_rejects():
    text = AIVEN_ROLES_FIXTURE.read_text(encoding="utf-8")
    assert text.count("SET avn_wal_sender_stop_timeout TO") == 3
    assert "\\restrict " in text and "\\unrestrict " in text and "GRANTED BY postgres" in text


def test_restore_test_roles_sql_drops_only_the_allowlisted_managed_settings():
    mod = _module()
    text = AIVEN_ROLES_FIXTURE.read_text(encoding="utf-8")
    filtered, skipped = mod.restore_test_roles_sql(text)
    assert len(skipped) == 29 and skipped == sorted(skipped)
    assert set(skipped) == set(mod.managed_setting_pairs())
    removed = [l for l in text.splitlines() if l.startswith("ALTER ROLE ") and " SET " in l]
    assert len(removed) == 29
    # Every other line, in order and byte for byte, is still there.
    assert filtered.splitlines() == [l for l in text.splitlines() if l not in removed]
    assert "avn_wal_sender_stop_timeout" not in filtered and " SET " not in filtered.replace("SET client_encoding", "") \
        .replace("SET standard_conforming_strings", "").replace("SET default_transaction_read_only = off", "")
    # Every role is still created with its attributes; every membership and grantor is kept.
    for role in ("_aiven", "_aiven_repl", "_aiven_rewind", "_avnadmin_auditing", "_avnadmin_managed",
                 "_avnadmin_monitor", "avnadmin", "avnadmin_group", "postgres"):
        assert f"CREATE ROLE {role};\nALTER ROLE {role} WITH " in filtered, role
    assert filtered.count("GRANT ") == text.count("GRANT ") == 10
    assert filtered.count("\\restrict ") == filtered.count("\\unrestrict ") == 1
    # The summary carries names only.
    info = mod.skipped_settings_info(skipped)
    assert info == f"skipped=29 sha256={mod.skipped_settings_digest(skipped)}"  # count and digest only


def test_the_packaged_roles_sql_is_the_unfiltered_dump():
    src = SCRIPT.read_text(encoding="utf-8")
    run = src[src.index("# ---- 3. isolated restore"):src.index("# ---- 4. verification")]
    assert "restore_test_roles_sql(roles_sql.read_text(" in run
    assert 'apply_roles_sql(bin_("psql"), restore_roles_path,' in run and "roles_sql.write" not in src
    pkg = src[src.index("# ---- 5. package"):]
    assert '"roles.sql": roles_sql,' in pkg and '"roles.sql": sha256_file(roles_sql)' in pkg
    assert "restore_roles_path" not in pkg and '"restore_test_skipped_role_settings": skipped_settings' in pkg
    # The comparison uses the symmetric view on both sides; the manifest keeps the complete one.
    assert 'dst_globals["roles_compared"] == src_globals["roles_compared"]' in run
    assert '"roles": src_globals["roles"],' in pkg


KEY = "Ab3" * 21  # 63 alphanumerics, pg_dump's \restrict key format
HEAD, TAIL = f"\\restrict {KEY}\n", f"\\unrestrict {KEY}\n"


def _wrapped(text):
    """`text` between the pg_dumpall restrict/unrestrict pair (check_psql_script requires it)."""
    return HEAD + text + TAIL


def _one(mod, text):
    filtered, skipped = mod.restore_test_roles_sql(_wrapped(text))
    assert filtered.startswith(HEAD) and filtered.endswith(TAIL)
    return filtered[len(HEAD):-len(TAIL)], skipped


@pytest.mark.parametrize("statement", [
    "ALTER ROLE _aiven_x SET avn_wal_sender_stop_timeout TO '10s';",            # look-alike, not a prefix match
    "ALTER ROLE _avnadmin_evil SET \"pgaudit.log\" TO 'none';",
    "ALTER ROLE _aiven_repl2 SET \"pgaudit.log\" TO 'none';",
    "ALTER ROLE \"_aiven\" SET \"pgaudit.log\" TO 'none';",                     # not pg_dumpall's spelling
    "ALTER ROLE \"_aiven \" SET \"pgaudit.log\" TO 'none';",
    "ALTER ROLE \"_AIVEN\" SET \"pgaudit.log\" TO 'none';",
    "ALTER ROLE avnadmin SET \"pgaudit.log\" TO 'none';",                       # application role
    "ALTER ROLE avnadmin_group SET avn_wal_sender_stop_timeout TO '10s';",
    "ALTER ROLE _aiven_rewind SET avn_wal_sender_stop_timeout TO '10s';",       # managed, no observed setting
    "ALTER ROLE _avnadmin_monitor SET avn_wal_sender_stop_timeout TO '10s';",   # param not observed there
    "ALTER ROLE _aiven SET search_path TO 'evil';",                             # param not allowlisted
    "ALTER ROLE _aiven SET \"avn_wal_sender_stop_timeout\" TO '10s';",          # not pg_dumpall's spelling
    "ALTER ROLE _aiven SET \"pgaudit.log.x\" TO 'none';",
    "ALTER ROLE _aiven IN DATABASE defaultdb SET \"pgaudit.log\" TO 'none';",   # IN DATABASE variant
    "ALTER ROLE _aiven RESET \"pgaudit.log\";",
    "ALTER ROLE _aiven WITH SUPERUSER;",
])
def test_anything_but_an_exact_allowlisted_pair_is_replayed_unchanged(statement):
    mod = _module()
    text = f"CREATE ROLE x;\n{statement}\nGRANT a TO b;\n"
    assert _one(mod, text) == (text, [])


def test_a_value_with_semicolons_and_newlines_never_moves_a_statement_boundary():
    mod = _module()
    # An application role's value that LOOKS like a managed statement: one statement, replayed.
    app = ("ALTER ROLE app SET \"pgaudit.log\" TO 'x;\nALTER ROLE _aiven SET \"pgaudit.log\" TO ''y'';\n';\n"
           "ALTER ROLE app2 SET application_name TO E'a\\';\\nALTER ROLE postgres SET \"pgaudit.log\" TO ''z'';';\n")
    assert _one(mod, app) == (app, [])
    # A managed statement whose value hides another statement: only that one statement goes.
    managed = ("ALTER ROLE _aiven SET \"pgaudit.log\" TO 'a;\nALTER ROLE app SET work_mem TO ''1MB'';';\n"
               "ALTER ROLE app SET work_mem TO '2MB';\n")
    assert _one(mod, managed) == ("ALTER ROLE app SET work_mem TO '2MB';\n", ["_aiven pgaudit.log"])
    # Two statements on one line: each is judged on its own.
    line = "ALTER ROLE _aiven SET \"pgaudit.log\" TO 'none'; ALTER ROLE app SET \"pgaudit.log\" TO 'none';\n"
    assert _one(mod, line) == (" ALTER ROLE app SET \"pgaudit.log\" TO 'none';\n", ["_aiven pgaudit.log"])


def test_quoted_identifiers_with_quotes_and_semicolons_are_kept_intact():
    mod = _module()
    text = ('CREATE ROLE "we;ird\'""name";\n'
            'ALTER ROLE "we;ird\'""name" SET "pgaudit.log" TO \'none\';\n'
            'GRANT "_aiven" TO "we;ird\'""name" GRANTED BY postgres;\n'
            'ALTER ROLE postgres SET "pg_stat_plans.track" TO \'none\';\n'
            '-- a comment; with ALTER ROLE _aiven SET "pgaudit.log" TO \'x\';\n')
    filtered, skipped = _one(mod, text)
    assert skipped == ["postgres pg_stat_plans.track"]
    assert filtered == text.replace('ALTER ROLE postgres SET "pg_stat_plans.track" TO \'none\';\n', "")


@pytest.mark.parametrize("text", [
    "ALTER ROLE app SET x TO $$a$$;\n",
    "ALTER ROLE app SET x TO 'a' /* c */;\n",
    "ALTER ROLE app SET x TO 'unterminated;\n",
    'ALTER ROLE "unterminated SET x TO \'a\';\n',
    "ALTER ROLE app SET x TO 'a'\n",                     # no final semicolon
    "ALTER ROLE app SET x TO 'a' \\gexec\n",             # a backslash outside a literal
    "  \\restrict key\n",                                # a meta command not at a line start
    "ALTER ROLE _aiven SET \"pgaudit.log\" TO 'a';\nALTER ROLE _aiven SET \"pgaudit.log\" TO 'b';\n",  # duplicate
])
def test_anything_the_lexer_cannot_classify_fails_closed(text):
    mod = _module()
    with pytest.raises(mod.BackupError) as exc:
        mod.restore_test_roles_sql(_wrapped(text))
    assert exc.value.code == "ROLES_SQL_UNPARSEABLE"


def test_unknown_settings_still_fail_with_invalid_setting():
    mod = _module()
    stderr = 'psql:roles.sql:43: ERROR:  unrecognized configuration parameter "avn_wal_sender_stop_timeout"'
    assert mod.error_class(stderr) == "INVALID_SETTING"


def test_roles_comparison_sql_filters_only_the_given_pairs():
    mod = _module()
    assert "%(skip)s::pg_catalog.text[]" in mod.ROLES_SQL and mod.ROLES_SQL.count("%(skip)s") == 1
    assert "pg_catalog.split_part(u.c, '='::pg_catalog.text, 1)" in mod.ROLES_SQL
    assert "WITH ORDINALITY" in mod.ROLES_SQL and "ORDER BY u.n" in mod.ROLES_SQL


def test_runbook_explains_the_managed_roles_restore_fix():
    text = RUNBOOK.read_text(encoding="utf-8")
    for needle in ("FCB-WP009-AIVEN-MANAGED-ROLES-RESTORE-FIX", "AIVEN_MANAGED_ROLE_SETTINGS", "avn_wal_sender_stop_timeout",
                   "identico byte per byte", "roles_sql_aiven_managed_settings_skipped_in_restore_test",
                   "restore_test_skipped_role_settings", "ROLES_SQL_UNPARSEABLE", "Limiti della verifica dei ruoli gestiti",
                   "Non eseguire `roles.sql` per intero", "roles_local.sql", "_aiven_x"):
        assert needle in text, needle



# ------------------------------------------------------------------ review of PR #204 (F1-F4)

FIXTURE_TEXT = AIVEN_ROLES_FIXTURE.read_text(encoding="utf-8") if AIVEN_ROLES_FIXTURE.exists() else ""
FIXTURE_KEY = re.search(r"^\\restrict (\S+)$", FIXTURE_TEXT, re.M).group(1) if FIXTURE_TEXT else ""


def _fixture_with(old, new):
    assert FIXTURE_TEXT.count(old) == 1, old
    return FIXTURE_TEXT.replace(old, new)


@pytest.mark.parametrize("text", [
    # F1: any meta-command other than the pg_dumpall pair, anywhere
    _fixture_with("CREATE ROLE _aiven;\n", "\\! touch /tmp/pwned\nCREATE ROLE _aiven;\n"),
    _fixture_with("CREATE ROLE _aiven;\n", "\\i /etc/passwd\nCREATE ROLE _aiven;\n"),
    _fixture_with("CREATE ROLE _aiven;\n", "\\set ON_ERROR_STOP 0\nCREATE ROLE _aiven;\n"),
    _fixture_with("CREATE ROLE _aiven;\n", "\\c template1\nCREATE ROLE _aiven;\n"),
    _fixture_with("CREATE ROLE _aiven;\n", "\\connect template1\nCREATE ROLE _aiven;\n"),
    # F1: the pair itself: missing, extra, wrong order, different keys, bad key, \\ separator, trailing text
    FIXTURE_TEXT.replace(f"\\restrict {FIXTURE_KEY}\n", ""),
    FIXTURE_TEXT.replace(f"\\unrestrict {FIXTURE_KEY}\n", ""),
    FIXTURE_TEXT.replace(f"\\restrict {FIXTURE_KEY}\n", "").replace(f"\\unrestrict {FIXTURE_KEY}\n", ""),
    _fixture_with("CREATE ROLE _aiven;\n", f"\\restrict {FIXTURE_KEY}\nCREATE ROLE _aiven;\n"),
    _fixture_with(f"\\unrestrict {FIXTURE_KEY}\n", f"\\unrestrict {'Z' * 63}\n"),
    FIXTURE_TEXT.replace(FIXTURE_KEY, "short"),
    FIXTURE_TEXT.replace(FIXTURE_KEY, FIXTURE_KEY[:-1] + "!"),
    _fixture_with(f"\\restrict {FIXTURE_KEY}\n", f"\\restrict {FIXTURE_KEY} \\\\ \\! id\n"),
    _fixture_with(f"\\restrict {FIXTURE_KEY}\n", f"\\restrict {FIXTURE_KEY} extra\n"),
    _fixture_with(f"\\unrestrict {FIXTURE_KEY}\n", f"\\unrestrict {FIXTURE_KEY}\\\\\\! id\n"),
    # F1: a statement outside the restricted section
    _fixture_with(f"\\restrict {FIXTURE_KEY}\n", f"CREATE ROLE early;\n\\restrict {FIXTURE_KEY}\n"),
    _fixture_with(f"\\unrestrict {FIXTURE_KEY}\n", f"\\unrestrict {FIXTURE_KEY}\nCREATE ROLE late;\n"),
    # F2: the lexer reads literals with standard_conforming_strings=on only
    _fixture_with("SET standard_conforming_strings = on;", "SET standard_conforming_strings = off;"),
    _fixture_with("SET client_encoding = 'UTF8';", "SET client_encoding = 'UTF8';\nSET standard_conforming_strings TO off;"),
    _fixture_with("SET client_encoding = 'UTF8';",
                  "SET client_encoding = 'UTF8';\nSELECT pg_catalog.set_config('standard_conforming_strings', 'off', false);"),
    _fixture_with("SET client_encoding = 'UTF8';", "SET client_encoding = 'UTF8';\nset Standard_Conforming_Strings = 'on';"),
], ids=["shell", "include", "set-var", "c", "connect", "no-restrict", "no-unrestrict", "no-pair", "two-restrict",
        "key-mismatch", "key-short", "key-charset", "restrict-separator", "restrict-trailing", "unrestrict-separator",
        "stmt-before", "stmt-after", "scs-off", "scs-to-off", "scs-set-config", "scs-other-spelling"])
def test_f1_f2_only_the_pg_dumpall_meta_pair_and_scs_on_are_accepted(text):
    mod = _module()
    with pytest.raises(mod.BackupError) as exc:
        mod.restore_test_roles_sql(text)
    assert exc.value.code == "ROLES_SQL_UNPARSEABLE"


def test_f1_the_real_dump_shape_is_accepted():
    mod = _module()
    segments = mod.split_psql_script(FIXTURE_TEXT)
    mod.check_psql_script(segments)
    assert [s for k, s in segments if k == "meta"] == [f"\\restrict {FIXTURE_KEY}\n", f"\\unrestrict {FIXTURE_KEY}\n"]
    assert len(FIXTURE_KEY) == 63


def test_f1_the_check_runs_before_psql():
    src = SCRIPT.read_text(encoding="utf-8")
    body = src[src.index("def restore_test_roles_sql"):src.index("def skipped_settings_info")]
    assert body.index("check_psql_script(segments)") < body.index("for kind, seg in segments")
    run = src[src.index("# ---- 3. isolated restore"):src.index("# ---- 4. verification")]
    assert run.index("restore_test_roles_sql(") < run.index("cluster.start()") < run.index("apply_roles_sql(")


@pytest.mark.parametrize("value", [
    "'10s' , '20s'", "'a'||'b'", "'a' 'b'", "now()", "'a', b", "'a'\n", "$x$", "'a' -- c\n", "1 + 1", "'a',",
    "E'a'", "(1)",
])
def test_f3_a_value_outside_pg_dumpall_format_is_never_skipped(value):
    mod = _module()
    stmt = f"ALTER ROLE _aiven SET \"pgaudit.log\" TO {value};"
    assert mod.managed_setting_of(stmt) is None


@pytest.mark.parametrize("value", ["'none'", "'it''s'", "'a;\nb'", "'ddl', 'role'", "on", "10s", "-1", "0.5"])
def test_f3_pg_dumpall_value_forms_are_skipped(value):
    mod = _module()
    assert mod.managed_setting_of(f"ALTER ROLE _aiven SET \"pgaudit.log\" TO {value};") == ("_aiven", "pgaudit.log")


@pytest.mark.parametrize("stderr, cls", [
    ("psql:/w/roles_restore_test.sql:7: error: backslash commands are restricted; only \\unrestrict is allowed",
     "META_COMMAND_REFUSED"),
    ("psql:/w/roles_restore_test.sql:7: error: invalid command \\foo", "META_COMMAND_REFUSED"),
    ("psql:/w/roles_restore_test.sql:7: error: could not open file", "OTHER"),
])
def test_f4_psql_client_errors_fail_even_with_exit_code_zero(monkeypatch, tmp_path, stderr, cls):
    mod = _module()
    import subprocess as sp
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: sp.CompletedProcess(a[0], 0, "", stderr + "\n"))
    with pytest.raises(mod.BackupError) as exc:
        mod.apply_roles_sql("psql", tmp_path / "r.sql", {}, "postgres")
    assert (exc.value.code, exc.value.detail) == ("ROLES_SQL_APPLY_FAILED", f"class={cls}")


def test_f4_the_expected_bootstrap_error_alone_still_passes(monkeypatch, tmp_path):
    mod = _module()
    import subprocess as sp
    err = 'psql:/w/roles_restore_test.sql:32: ERROR:  role "postgres" already exists\n'
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: sp.CompletedProcess(a[0], 0, "", err))
    mod.apply_roles_sql("psql", tmp_path / "r.sql", {}, "postgres")



# ------------------------------------------------------------------ owner ratification FCB-WP009-PR204-F5-RUNBOOK

def _fixture_skipped():
    return _module().restore_test_roles_sql(AIVEN_ROLES_FIXTURE.read_text(encoding="utf-8"))[1]


MANAGED_WORDS = sorted(SERVICE_PARAMS | MONITOR_PARAMS | {"_aiven", "_aiven_repl"})  # leak-check words


def _names(word, text):
    return re.search(rf"(?<![A-Za-z0-9_$.]){re.escape(word)}(?![A-Za-z0-9_$])", text)


def test_f5_public_summary_row_is_count_and_digest_only():
    mod = _module()
    skipped = _fixture_skipped()
    info = mod.skipped_settings_info(skipped)
    assert re.fullmatch(r"skipped=29 sha256=[0-9a-f]{64}", info), info
    for word in MANAGED_WORDS + ["_avnadmin_monitor", "postgres", "10s", "read committed", "none"]:
        assert not _names(word, info), word
    assert mod.skipped_settings_info([]) == "skipped=0 sha256=" + hashlib.sha256(b"[]").hexdigest()


def test_f5_digest_is_the_documented_canonical_encoding_deterministic_and_sensitive():
    mod = _module()
    skipped = _fixture_skipped()
    canonical = json.dumps(sorted(x.split(" ", 1) for x in skipped), ensure_ascii=True, separators=(",", ":"))
    assert canonical.startswith('[["_aiven","avn_wal_sender_stop_timeout"],')
    digest = mod.skipped_settings_digest(skipped)
    assert digest == hashlib.sha256(canonical.encode()).hexdigest()
    assert mod.skipped_settings_digest(list(reversed(skipped))) == digest           # order-independent
    assert mod.skipped_settings_digest(skipped[1:]) != digest                       # a pair removed
    changed = ["_aiven_repl pgaudit.log" if x == "_aiven pgaudit.log" else x for x in skipped]
    assert mod.skipped_settings_digest(changed + ["_aiven pgaudit.logx"]) != digest  # a pair changed
    # Unambiguous: the split between role and parameter matters.
    assert mod.skipped_settings_digest(["a b.c"]) != mod.skipped_settings_digest(["a.b c"])
    src = SCRIPT.read_text(encoding="utf-8")
    pkg = src[src.index("# ---- 5. package"):]
    assert '"restore_test_skipped_role_settings_sha256": skipped_settings_digest(skipped_settings)' in pkg


@pytest.mark.parametrize("word", MANAGED_WORDS)
def test_f5_leak_check_refuses_a_summary_naming_a_managed_pair(tmp_path, word):
    mod = _module()
    summary = tmp_path / "s.md"
    summary.write_text(f"| roles_sql_aiven_managed_settings_skipped_in_restore_test | PASS | skipped=1 x[{word}] |\n")
    assert "SUMMARY_NAMES_MANAGED_ROLE_SETTINGS" in mod.leak_problems(summary, None, AIVEN_ENV)


def test_f5_leak_check_passes_the_new_row_and_has_no_false_positive(tmp_path):
    mod = _module()
    summary = tmp_path / "s.md"
    summary.write_text("## FCB defaultdb encrypted backup: PASS\n"
                       f"| roles_sql_aiven_managed_settings_skipped_in_restore_test | PASS | "
                       f"{mod.skipped_settings_info(_fixture_skipped())} |\n"
                       "- pg_dump: `pg_dump (PostgreSQL) 18.6`\n- extensions: `0`\n- excluded_schemas: `1`\n")
    # The DSN user `avnadmin` and the bootstrap rule are unaffected; nothing here is a leak.
    assert mod.leak_problems(summary, None, AIVEN_ENV) == []


def test_f5_a_failed_run_summary_names_no_managed_pair(tmp_path, capsys):
    mod = _module()
    checks = mod.Checks()
    checks.add("roles_sql_aiven_managed_settings_skipped_in_restore_test", True,
               mod.skipped_settings_info(_fixture_skipped()))
    checks.add("error", False, "ROLES_SQL_APPLY_FAILED class=INVALID_SETTING")
    mod._write_summary(str(tmp_path / "s.md"), checks, {})
    text = (tmp_path / "s.md").read_text()
    assert mod.leak_problems(tmp_path / "s.md", None, AIVEN_ENV) == []
    for word in MANAGED_WORDS + ["_avnadmin_monitor", "postgres"]:
        assert not _names(word, text), word


def test_f5_runbook_documents_codes_families_residual_p3_and_digest_check():
    text = RUNBOOK.read_text(encoding="utf-8")
    for needle in ("FCB-WP009-PR204-F5-RUNBOOK", "skipped=<N> sha256=<digest>", 'separators=(",", ":")',
                   "SUMMARY_NAMES_MANAGED_ROLE_SETTINGS", "restore_test_skipped_role_settings_sha256",
                   "Errori dei ruoli", "**(a) Errore nel file esportato**", "**(b) Compatibilità con Aiven**",
                   "**(c) Fallimento del restore isolato**", "class=META_COMMAND_REFUSED", "class=INVALID_SETTING",
                   "class=OTHER", "\\restrict <key>", "63 caratteri alfanumerici", "SET standard_conforming_strings = on;",
                   "dollar quoting", "P3 residui accettati", "U&'…'", "set_config", "E'…'",
                   "Confronto del digest pubblico con il manifest"):
        assert needle in text, needle
    assert "le coppie ruolo/parametro dell'allowlist nella riga" not in text
    # The documented recipe gives the code's digest.
    mod = _module()
    skipped = _fixture_skipped()
    c = json.dumps(sorted(x.split(" ", 1) for x in skipped), ensure_ascii=True, separators=(",", ":"))
    assert hashlib.sha256(c.encode()).hexdigest() == mod.skipped_settings_digest(skipped)


# ------------------------------------------------------------------ PATH A2 R1 (FCB-A2-R1-WP009-DECISIONS)

def test_r1_backup_admits_fcb_cert_quota_only_when_exact_and_dumps_it():
    mod = _module()
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_aiven_migrations as gen
    assert mod.CERT_QUOTA_STATE_SQL == gen.cert_quota_state_one_line()
    assert "(" + mod.CERT_QUOTA_STATE_SQL + ") AS cert_quota_state" in mod.GUARD_SQL
    base = {"db": "defaultdb", "ver": 180006, "ssl": True, "same_user": True, "read_only": "on",
            "isolation": "repeatable read", "own_other_schemas": [], "foreign_owned": 0, "acting_members": 0,
            "large_objects": 0, "forced_rls": 0, "extensions_outside_public": 0}
    for state, ok in (("EXACT", True), ("DRIFT", False), ("ABSENT", False), (None, False)):
        problems, excluded = mod.check_guard({**base, "other_schemas": ["fcb_cert_quota"], "cert_quota_state": state},
                                             expected_db="defaultdb", require_tls=True)
        assert (problems == []) is ok and excluded == [], state  # admitted means DUMPED, never excluded
    problems, _ = mod.check_guard({**base, "other_schemas": None, "cert_quota_state": "ABSENT"},
                                  expected_db="defaultdb", require_tls=True)
    assert problems == []
    # EXACT admits that one schema only
    problems, _ = mod.check_guard({**base, "other_schemas": ["fcb_cert_quota", "x"], "cert_quota_state": "EXACT"},
                                  expected_db="defaultdb", require_tls=True)
    assert problems == ["UNEXPECTED_SCHEMA"]
    src = SCRIPT.read_text(encoding="utf-8")
    assert '"restore_cert_quota_state"' in src and 'src.get("cert_quota_state") in ("ABSENT", "EXACT")' in src
