"""FCB-WP-009: static checks of the owner-gated roles workflow and its SQL/helper (no network).

The behaviour on real PostgreSQL is in tests/integration/test_wp009_roles_runtime_postgres.py;
the G-02 secret policy (gates before any database secret) in tests/test_workflow_secret_policy.py.
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

ROOT = Path(__file__).resolve().parents[1]
WF_PATH = ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-roles.yml"
RESTORE_PATH = ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-restore.yml"
AIVEN = ROOT / "sql" / "aiven"
HELPER = ROOT / "scripts" / "fcb_db_roles.py"
WF = yaml.safe_load(WF_PATH.read_text(encoding="utf-8"))
JOB = WF["jobs"]["roles"]
STEPS = JOB["steps"]
CONFIRM = "APPLY WP-009 ROLES TO defaultdb"
TX_FILES = ("roles_apply_guard.sql", "upsert_bridge_roles.sql", "prepare_bridge_roles.sql",
            "finalize_bridge_privileges.sql", "roles_post_checks.sql", "assert_bridge_roles.sql")
NEW_SQL = ("roles_apply_guard.sql", "upsert_bridge_roles.sql", "assert_bridge_roles.sql",
           "verify_bridge_roles.sql", "diagnose_bridge_roles.sql", "prepare_bridge_roles.sql",
           "finalize_bridge_privileges.sql", "roles_post_checks.sql")
PASSWORD_SECRETS = ("FCB_APP_DB_PASSWORD", "FCB_MIGRATOR_DB_PASSWORD")


def _step(prefix):
    return next(s for s in STEPS if s.get("name", "").startswith(prefix))


def _helper():
    spec = importlib.util.spec_from_file_location("fcb_db_roles_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql_only(text):
    """SQL text without comments and without psql meta-command lines."""
    text = re.sub(r"--[^\n]*", "", text)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("\\"))


# ---------------------------------------------------------------- triggers, gates, modes

def test_manual_dispatch_only_with_diagnose_default_and_confirm_input():
    triggers = WF.get("on", WF.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["options"] == ["diagnose", "apply"] and inputs["mode"]["default"] == "diagnose"
    assert inputs["confirm"]["default"] == ""
    assert WF["permissions"] == {"contents": "read", "actions": "read"}


def test_job_runs_on_main_in_the_protected_restore_environment_serialized_with_the_restore():
    assert JOB["if"] == "github.ref == 'refs/heads/main'"
    assert JOB["environment"] == "aiven-production"
    restore = yaml.safe_load(RESTORE_PATH.read_text(encoding="utf-8"))
    assert WF["concurrency"] == restore["concurrency"]
    assert WF["concurrency"]["cancel-in-progress"] is False
    assert JOB["env"]["CONFIRM_PHRASE"] == CONFIRM
    assert JOB["env"]["EXPECTED_DB"] == "defaultdb"


def test_the_two_g02_gates_are_the_first_steps_after_checkout():
    assert STEPS[0]["uses"].startswith("actions/checkout@") and STEPS[0]["with"]["persist-credentials"] is False
    env_gate, dispatch_gate = STEPS[1]["run"], STEPS[2]["run"]
    assert 'reviewers="$(bash scripts/require_protected_environment.sh "$REPO" aiven-production)"' in env_gate
    assert dispatch_gate.splitlines()[1].strip() == "bash scripts/require_owner_main_dispatch.sh"
    for step in STEPS[:3]:
        assert not any(k in step for k in ("if", "continue-on-error"))


def test_apply_requires_the_exact_phrase_and_diagnose_never_reaches_a_write():
    gate = _step("Mode and confirmation gate")["run"]
    assert 'if [ "$CONFIRM" != "$CONFIRM_PHRASE" ]' in gate
    apply = _step("Apply roles")
    assert apply["if"] == f"${{{{ inputs.mode == 'apply' && inputs.confirm == '{CONFIRM}' }}}}"
    assert _step("Diagnose mode")["if"] == "${{ inputs.mode != 'apply' }}"
    # Only the apply step opens a writable session; every other psql session is read-only.
    report = _step("Prechecks and roles report")
    assert "default_transaction_read_only=on" in report["env"]["PGOPTIONS"]
    assert 'ro="$(qf -c "select pg_catalog.current_setting(\'transaction_read_only\')")"' in report["run"]
    for step in STEPS:
        if step is not apply:
            assert "-1" not in re.findall(r"\s(-1)\s", str(step.get("run", ""))), step.get("name")


# ---------------------------------------------------------------- TLS, target, sessions

def test_tls_verify_full_with_the_authenticated_ca_and_pinned_target():
    ca = _step("Materialize and authenticate the Aiven project CA")["run"]
    assert 'test "$actual" = "$FCB_AIVEN_CA_SHA256"' in ca and 'PGSSLROOTCERT=$ca' in ca
    mask = _step("Mask connection details and enforce TLS")["run"]
    assert "if mode != 'verify-full'" in mask
    assert "python -I -S scripts/aiven_pg18_diag.py target" in mask
    restore_env = yaml.safe_load(RESTORE_PATH.read_text(encoding="utf-8"))["jobs"]["restore"]["env"]
    for key in ("EXPECTED_HOST_SHA256", "EXPECTED_PORT", "EXPECTED_PORT_SHA256", "PGPORT", "EXPECTED_COUNTS"):
        assert JOB["env"][key] == restore_env[key], key
    for step in STEPS:
        env = step.get("env") or {}
        if "FCB_DEFAULTDB_URL" in env and "PSQL=" in str(step.get("run", "")):
            assert env["PGSSLMODE"] == "verify-full", step["name"]
            assert env["PGOPTIONS"].startswith("-c search_path=public -c quote_all_identifiers=off"), step["name"]


def _guard(path, job, prefix):
    wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    run = next(s for s in wf["jobs"][job]["steps"] if s.get("name", "").startswith(prefix))["run"]
    return next(l.strip() for l in run.splitlines() if l.strip().startswith('GUARD="DO'))


def test_target_guard_is_byte_identical_to_the_restore_guard():
    assert _guard(WF_PATH, "roles", "Apply roles") == _guard(RESTORE_PATH, "restore", "Restore bridge")


def test_one_transaction_runs_the_files_in_the_tested_order_twice_with_evidence_checks():
    run = _step("Apply roles")["run"]
    files = " ".join(f"-f sql/aiven/{f}" for f in TX_FILES)
    assert f'-1 -c "$GUARD" {files} >/dev/null' in " ".join(run.replace("\\\n", " ").split())
    assert run.count("run;") == 2
    assert 'test "$first" = "$second"' in run and 'test "$schema_before" = "$schema_after"' in run
    assert 'test "$roles_after" = "HARDENED"' in run
    assert "login-check --require-tls" in run
    # Re-check right before writing: BRIDGE_0001_0008, or an already hardened model.
    assert ('if [ "$roles_before" != "HARDENED" ] && { [ "$before" != "BRIDGE_0001_0008" ] '
            '|| [ "$roles_before" != "ROLES_ABSENT" ]; }; then') in run


def test_every_sql_file_the_workflow_runs_is_pinned_by_sha256():
    pinned = {}
    for line in JOB["env"]["EXPECTED_SQL_SHA256"].splitlines():
        if line.strip():
            path, sha = line.strip().split("=")
            pinned[path] = sha
    for path, sha in pinned.items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == sha, f"re-pin {path}"
    text = WF_PATH.read_text(encoding="utf-8")
    used = set(re.findall(r"sql/aiven/[a-z0-9_]+\.sql", text.split("steps:", 1)[1]))
    included = {f"sql/aiven/{m}" for f in pinned for m in re.findall(r"\\ir\s+(\S+)", (ROOT / f).read_text())}
    assert used | included <= set(pinned), sorted((used | included) - set(pinned))
    assert f'test "$n" -eq {len(pinned)}' in _step("Verify the pinned SHA-256")["run"]


# ---------------------------------------------------------------- secrets

def test_role_passwords_reach_only_apply_steps_and_never_a_command_line():
    for step in STEPS:
        env = step.get("env") or {}
        run = str(step.get("run", ""))
        for name in PASSWORD_SECRETS:
            if name in env:
                assert env[name] == f"${{{{ secrets.{name} }}}}"
                assert "inputs.mode == 'apply'" in step.get("if", ""), step["name"]
            assert f"${name}" not in run and f"${{{name}" not in run, step["name"]
    apply = _step("Apply roles")["run"]
    for var in ("app_verifier", "migrator_verifier"):
        assert f'{var}="$(python -I -S scripts/fcb_db_roles.py verifier ' in apply
        # Never written to stdout, not even as a mask command (owner requirement).
        assert not re.search(rf"(echo|printf)[^\n]*\${{?{var}", apply), var
    # Verifiers go to psql only through its environment (\getenv), never via -v or argv.
    assert 'FCB_APP_DB_VERIFIER="$app_verifier" FCB_MIGRATOR_DB_VERIFIER="$migrator_verifier"' in apply
    assert "-v fcb_app_verifier" not in apply and "verifier=" not in apply.replace("_verifier=", "")
    assert "GITHUB_ENV" not in apply


def test_psql_stderr_is_only_printed_after_redaction():
    for step in STEPS:
        run = str(step.get("run", ""))
        if '2>"$err"' in run:
            assert "scripts/fcb_db_roles.py sanitize \"$err\"" in run, step["name"]
            assert 'cat "$err"' not in run


def test_upsert_reads_verifiers_from_the_environment_and_embeds_no_credential():
    text = (AIVEN / "upsert_bridge_roles.sql").read_text()
    assert "\\getenv fcb_app_verifier FCB_APP_DB_VERIFIER" in text
    assert "\\getenv fcb_migrator_verifier FCB_MIGRATOR_DB_VERIFIER" in text
    assert "ALTER ROLE fcb_app WITH LOGIN PASSWORD :'fcb_app_verifier';" in text
    assert "ALTER ROLE fcb_migrator WITH LOGIN PASSWORD :'fcb_migrator_verifier';" in text
    assert "ALTER ROLE fcb_signer WITH NOLOGIN PASSWORD NULL;" in text
    assert not re.search(r"PASSWORD\s+'", text), "literal password"
    assert "SCRAM-SHA-256$4096:" not in text.replace("^SCRAM-SHA-256\\$4096:", "")
    assert "GRANT fcb_migrator TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;" in text
    # Aiven's server-level createrole_self_grant='inherit,set' is disabled before any CREATE ROLE.
    code = _sql_only(text)
    assert code.index("SET LOCAL createrole_self_grant = '';") < code.index("CREATE ROLE")
    assert "FCB_ROLES_SELF_GRANT_NOT_DISABLED" in text
    for forbidden in ("SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS", "TO fcb_signer"):
        assert forbidden not in _sql_only(text).replace("NOLOGIN", ""), forbidden


# ---------------------------------------------------------------- SQL hygiene

@pytest.mark.parametrize("name", NEW_SQL)
def test_role_sql_never_drops_or_changes_data(name):
    sql = re.sub(r"'[^']*'", "''", _sql_only((AIVEN / name).read_text())).upper()  # literals are data
    for word in ("DROP ", "TRUNCATE", "REASSIGN OWNED", "DELETE FROM", "UPDATE ", "INSERT ", "COPY ",
                 "ALTER ROLE AVNADMIN", "SET ROLE", "SECURITY DEFINER", "CREATE TABLE", "CREATE FUNCTION"):
        assert word not in sql, (name, word)


@pytest.mark.parametrize("name", NEW_SQL)
def test_role_sql_is_catalog_qualified(name):
    sql = _sql_only((AIVEN / name).read_text())
    sql = re.sub(r"'[^']*'", "''", sql)  # string literals (incl. table names) are not references
    sql = re.sub(r"(?m)^\s*SET LOCAL [a-z_]+ = [^;]*;$", "", sql)  # GUC assignments, not operators
    for match in re.finditer(r"(?<![\w.])(pg_[a-z_]+|current_setting|set_config|format|unnest|aclexplode|"
                             r"acldefault|count|string_agg|has_[a-z_]+_privilege)\s*(?=[\s(,)]|$)", sql):
        token = match.group(1)
        if token == "pg_database_owner":
            continue
        raise AssertionError(f"{name}: unqualified {token}")
    for op in (" = ", " <> ", " != ", " ~ ", " || "):
        assert op not in sql, (name, op)


# ---------------------------------------------------------------- helper script

GOOD = {"FCB_APP_DB_PASSWORD": "a" * 20 + "B" * 20, "FCB_MIGRATOR_DB_PASSWORD": "c" * 20 + "_-" * 10,
        "FCB_DEFAULTDB_URL": "postgresql://avnadmin:AdminSecret123@db.example.invalid:14274/defaultdb?sslmode=verify-full"}


@pytest.mark.parametrize("change,category", [
    ({"FCB_APP_DB_PASSWORD": ""}, "FCB_APP_DB_PASSWORD_missing"),
    ({"FCB_MIGRATOR_DB_PASSWORD": "short"}, "FCB_MIGRATOR_DB_PASSWORD_not_32_to_128_url_safe_chars"),
    ({"FCB_APP_DB_PASSWORD": "x" * 31 + "@"}, "FCB_APP_DB_PASSWORD_not_32_to_128_url_safe_chars"),
    ({"FCB_APP_DB_PASSWORD": "x" * 40 + "\n"}, "FCB_APP_DB_PASSWORD_not_32_to_128_url_safe_chars"),
    ({"FCB_MIGRATOR_DB_PASSWORD": GOOD["FCB_APP_DB_PASSWORD"]}, "passwords_not_distinct"),
    ({"FCB_DEFAULTDB_URL": f"postgresql://avnadmin:{GOOD['FCB_APP_DB_PASSWORD']}@h/defaultdb"},
     "password_equals_administrative_password"),
])
def test_password_policy_fails_closed_with_categories_only(change, category):
    assert _helper().password_problem({**GOOD, **change}) == category


def test_password_policy_accepts_good_values_and_verifier_is_well_formed_and_salted():
    helper = _helper()
    assert helper.password_problem(GOOD) is None
    v1, v2 = (helper.scram_verifier(GOOD["FCB_APP_DB_PASSWORD"]) for _ in range(2))
    assert helper.VERIFIER_RE.fullmatch(v1) and v1 != v2
    assert GOOD["FCB_APP_DB_PASSWORD"] not in v1
    # Deterministic for a fixed salt (RFC 7677 construction, PostgreSQL's stored format).
    fixed = helper.scram_verifier("pencil", salt=b"\x00" * 16)
    assert fixed == helper.scram_verifier("pencil", salt=b"\x00" * 16)
    assert fixed.startswith("SCRAM-SHA-256$4096:AAAAAAAAAAAAAAAAAAAAAA==$")


def test_cli_never_prints_a_password_and_refuses_weak_input():
    env = {"PATH": "/usr/bin:/bin", **GOOD}
    out = subprocess.run([sys.executable, "-I", "-S", str(HELPER), "check"], env=env, capture_output=True, text=True)
    assert out.returncode == 0 and GOOD["FCB_APP_DB_PASSWORD"] not in out.stdout + out.stderr
    out = subprocess.run([sys.executable, "-I", "-S", str(HELPER), "verifier", "FCB_APP_DB_PASSWORD"], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("SCRAM-SHA-256$4096:")
    assert GOOD["FCB_APP_DB_PASSWORD"] not in out.stdout
    weak = {**env, "FCB_APP_DB_PASSWORD": "weak"}
    for args in (["check"], ["verifier", "FCB_APP_DB_PASSWORD"]):
        out = subprocess.run([sys.executable, "-I", "-S", str(HELPER), *args], env=weak, capture_output=True, text=True)
        assert out.returncode == 1 and out.stdout == "" and "weak" not in out.stderr
    out = subprocess.run([sys.executable, "-I", "-S", str(HELPER), "verifier", "FCB_DEFAULTDB_URL"], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 2 and out.stdout == ""


def test_sanitize_redacts_uri_parts_passwords_verifiers_and_statement_echo(tmp_path):
    helper = _helper()
    verifier = helper.scram_verifier(GOOD["FCB_APP_DB_PASSWORD"])
    env = {**GOOD, "FCB_APP_DB_VERIFIER": verifier}
    text = (f"psql: error: connection to server at \"db.example.invalid\" port 14274 failed: x\n"
            f"ERROR:  syntax error at or near \"{verifier}\"\n"
            f"LINE 1: ALTER ROLE fcb_app WITH LOGIN PASSWORD '{verifier}'\n"
            f"                                                ^\n"
            f"ERROR:  FCB_ROLES_OWNER_GUARD_FAILED password {GOOD['FCB_MIGRATOR_DB_PASSWORD']}\n"
            f"detail AdminSecret123 avnadmin\n")
    out = helper.sanitize(text, env)
    for secret in (verifier, GOOD["FCB_APP_DB_PASSWORD"], GOOD["FCB_MIGRATOR_DB_PASSWORD"], "AdminSecret123",
                   "avnadmin", "db.example.invalid", "14274", "SCRAM"):
        assert secret not in out, secret
    assert "\n" not in out and len(out) <= 300
    # The FCB_ code of an in-transaction guard is kept unless its line carries a secret.
    out = helper.sanitize("ERROR:  FCB_ROLES_OWNER_GUARD_FAILED\nCONTEXT: PL/pgSQL", env)
    assert "FCB_ROLES_OWNER_GUARD_FAILED" in out


# ---------------------------------------------------------------- fingerprints and memberships

def test_structure_fingerprint_is_the_restore_fingerprint_without_owner_and_acl_fields():
    helper = _helper()
    full = (AIVEN / "fingerprint_bridge_schema.sql").read_text(encoding="utf-8")
    committed = (AIVEN / "fingerprint_bridge_structure.sql").read_text(encoding="utf-8")
    assert committed == helper.structure_fingerprint_sql(full), "regenerate: python scripts/fcb_db_roles.py structure-fingerprint-sql"
    body = committed.split("WITH s AS", 1)[1]
    for gone in ("c.relacl", "c.relowner", "p.proacl", "p.proowner"):
        assert gone not in body, gone
    assert "a.attacl" in body  # column privileges are still fingerprinted (must stay NULL)


def test_first_apply_requires_the_restored_fingerprint_and_structure_must_not_change():
    restore_env = yaml.safe_load(RESTORE_PATH.read_text(encoding="utf-8"))["jobs"]["restore"]["env"]
    assert JOB["env"]["EXPECTED_FINGERPRINT"] == restore_env["EXPECTED_FINGERPRINT"]
    run = _step("Apply roles")["run"]
    assert 'test "$full_before" = "$EXPECTED_FINGERPRINT"' in run
    assert 'test "$structure_before" = "$structure_after"' in run
    assert run.index("structure_before=") < run.index("run;    first=") < run.index("structure_after=")


def test_the_model_asserts_the_exact_membership_inventory_and_no_escalation_path():
    verify = (AIVEN / "verify_bridge_roles.sql").read_text()
    for code in ("membership_inventory", "escalation_path", "unexpected_app_or_signer_member",
                 "unexpected_migrator_member", "role_member_of_other_role"):
        assert f"'{code}'" in verify
    # The four expected rows: implicit ADMIN-only grants from the bootstrap superuser (oid 10),
    # and the one explicit INHERIT+SET grant to the session, by the session.
    assert verify.count("true, false, false, 10::pg_catalog.oid") == 3
    assert "false, true, true, (SELECT sess.oid FROM sess)" in verify
    diagnose = (AIVEN / "diagnose_bridge_roles.sql").read_text()
    assert "'membership '" in diagnose and "'effective '" in diagnose and "' grantor='" in diagnose


def test_login_check_runs_isolated_with_hash_pinned_dependencies():
    run = _step("Apply roles")["run"]
    assert "python -I scripts/fcb_db_roles.py login-check --require-tls" in run and "PYTHONPATH" not in run
    pins = _step("Verify the pinned SHA-256")["run"]
    assert "pip install --quiet --require-hashes --no-deps -r scripts/fcb_db_roles_requirements.txt" in pins
    req = (ROOT / "scripts" / "fcb_db_roles_requirements.txt").read_text()
    for pkg in ("psycopg==3.3.6", "psycopg-binary==3.3.6", "typing-extensions=="):
        block = req.split(pkg, 1)[1].split("\n\n", 1)[0]
        assert re.search(r"--hash=sha256:[0-9a-f]{64}", block.split("\n", 2)[1]), pkg


def test_every_administrative_session_outside_the_apply_transaction_is_read_only():
    """After hardening the administrator must not read bridge tables read-write (planted code)."""
    report = _step("Prechecks and roles report")
    assert "default_transaction_read_only=on" in report["env"]["PGOPTIONS"]
    run = _step("Apply roles")["run"]
    assert 'ro() { PGOPTIONS="$PGOPTIONS -c default_transaction_read_only=on" sp' in run
    for line in run.splitlines():
        if "$(sp " in line or "$(ro " in line:
            assert "$(ro " in line, line  # every captured query outside `run` is read-only


def test_model_pins_the_same_routines_and_triggers_as_the_restore_precheck():
    pre = (AIVEN / "defaultdb_precheck.sql").read_text()
    verify = (AIVEN / "verify_bridge_roles.sql").read_text()
    for start, end in (("bridge_functions(sig) AS (VALUES", "\n)"), ("bridge_triggers(sig) AS (VALUES", "\n)")):
        block = pre[pre.index(start):pre.index(end, pre.index(start)) + 2]
        assert block in verify, start
    for code in ("routine_set", "trigger_set", "relation_hooks", "foreign_objects", "role_settings"):
        assert f"'{code}'" in verify


def test_re_run_path_reads_no_bridge_table_as_the_administrator():
    guard = (AIVEN / "roles_apply_guard.sql").read_text()
    first, rerun = guard.split("\\if :fcb_first_run", 1)[1].split("\\else", 1)
    assert "FROM public.schema_migrations" in first and "public." not in _sql_only(rerun).replace("'public'", "")
    assert "FCB_ROLES_RERUN_NOT_HARDENED" in rerun and "\\ir verify_bridge_roles.sql" in rerun
    post = (AIVEN / "roles_post_checks.sql").read_text()
    assert "\\if :fcb_first_run\n\\ir assert_bridge_counts.sql\n\\endif" in post
