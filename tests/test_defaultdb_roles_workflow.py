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
CONFIRM_APPLY = "APPLY WP-009 ROLES TO defaultdb"
CONFIRM_ENABLE = "ENABLE WP-009 MIGRATOR ON defaultdb"
TX1_FILES = ("roles_apply_guard.sql", "upsert_bridge_roles.sql", "grant_bridge_runtime.sql",
             "assert_bridge_counts.sql", "assert_bridge_roles.sql")
TX2_FILES = ("enable_bridge_migrator.sql", "assert_bridge_roles.sql")
NEW_SQL = ("roles_apply_guard.sql", "upsert_bridge_roles.sql", "grant_bridge_runtime.sql",
           "enable_bridge_migrator.sql", "assert_bridge_roles.sql", "verify_bridge_roles.sql",
           "diagnose_bridge_roles.sql", "count_bridge_catalog.sql", "prepare_bridge_roles.sql",
           "finalize_bridge_privileges.sql")
PASSWORD_SECRETS = ("FCB_APP_DB_PASSWORD", "FCB_MIGRATOR_DB_PASSWORD")
PHASE1, PHASE2, EVIDENCE, PRECHECK = "Phase 1 -", "Phase 2 -", "Phase 2 evidence", "Prechecks and roles report"


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


def _flat(run):
    return " ".join(run.replace("\\\n", " ").split())


# ---------------------------------------------------------------- triggers, gates, modes

def test_manual_dispatch_only_three_modes_and_inputs():
    triggers = WF.get("on", WF.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["options"] == ["diagnose", "apply", "enable-migrator"] and inputs["mode"]["default"] == "diagnose"
    assert inputs["confirm"]["default"] == "" and inputs["e2e_run_id"]["default"] == ""
    assert WF["permissions"] == {"contents": "read", "actions": "read"}


def test_job_runs_on_main_in_the_protected_restore_environment_serialized_with_the_restore():
    assert JOB["if"] == "github.ref == 'refs/heads/main'"
    assert JOB["environment"] == "aiven-production"
    restore = yaml.safe_load(RESTORE_PATH.read_text(encoding="utf-8"))
    assert WF["concurrency"] == restore["concurrency"] and WF["concurrency"]["cancel-in-progress"] is False
    assert JOB["env"]["CONFIRM_APPLY"] == CONFIRM_APPLY and JOB["env"]["CONFIRM_ENABLE"] == CONFIRM_ENABLE
    assert JOB["env"]["EXPECTED_DB"] == "defaultdb" and int(JOB["env"]["OBSERVE_SECONDS"]) >= 60


def test_the_two_g02_gates_are_the_first_steps_after_checkout():
    assert STEPS[0]["uses"].startswith("actions/checkout@") and STEPS[0]["with"]["persist-credentials"] is False
    env_gate, dispatch_gate = STEPS[1]["run"], STEPS[2]["run"]
    assert 'reviewers="$(bash scripts/require_protected_environment.sh "$REPO" aiven-production)"' in env_gate
    assert dispatch_gate.splitlines()[1].strip() == "bash scripts/require_owner_main_dispatch.sh"
    for step in STEPS[:3]:
        assert not any(k in step for k in ("if", "continue-on-error"))


def test_each_write_step_requires_its_own_exact_phrase_and_diagnose_never_writes():
    gate = _step("Mode and confirmation gate")["run"]
    assert 'if [ "$CONFIRM" != "$CONFIRM_APPLY" ]' in gate and 'if [ "$CONFIRM" != "$CONFIRM_ENABLE" ]' in gate
    assert _step(PHASE1)["if"] == f"${{{{ inputs.mode == 'apply' && inputs.confirm == '{CONFIRM_APPLY}' }}}}"
    assert _step(PHASE2)["if"] == f"${{{{ inputs.mode == 'enable-migrator' && inputs.confirm == '{CONFIRM_ENABLE}' }}}}"
    assert _step(EVIDENCE)["if"] == _step(PHASE2)["if"]
    assert STEPS.index(_step(EVIDENCE)) < STEPS.index(_step(PHASE2))
    assert _step("Diagnose mode")["if"] == "${{ inputs.mode == 'diagnose' }}"
    for step in STEPS:  # only the two phase steps open a transaction (psql -1)
        if step.get("name", "").startswith((PHASE1, PHASE2)):
            continue
        assert not re.search(r"\s-1\s", str(step.get("run", ""))), step.get("name")


def test_precheck_accepts_exactly_the_documented_states_per_mode():
    run = _step(PRECHECK)["run"]
    assert ("diagnose:HARDENED|diagnose:HARDENED_PHASE1|apply:HARDENED_PHASE1|enable-migrator:HARDENED_PHASE1"
            "|enable-migrator:HARDENED)") in run
    assert 'if [ "$state" = "BRIDGE_0001_0008" ] && [ "$roles_state" = "ROLES_ABSENT" ]; then first_apply=true; fi' in run
    # The tracked precheck (it reads schema_migrations) only while no bridge role exists.
    assert 'test "$roles_state" = "ROLES_ABSENT" || { echo "::error::tracked precheck refused once bridge roles exist"; exit 1; }' in run


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


def test_target_guards_are_byte_identical_to_the_restore_guard():
    restore = _guard(RESTORE_PATH, "restore", "Restore bridge")
    assert _guard(WF_PATH, "roles", PHASE1) == restore and _guard(WF_PATH, "roles", PHASE2) == restore


def test_phase1_runs_its_files_in_the_tested_order_twice_with_evidence_checks():
    run = _step(PHASE1)["run"]
    files = " ".join(f"-f sql/aiven/{f}" for f in TX1_FILES)
    assert f'-v expected_roles_state=HARDENED_PHASE1 -1 -c "$GUARD" {files} >/dev/null' in _flat(run)
    assert run.count("run;") == 2 and 'test "$first" = "$second"' in run
    assert 'test "$structure_before" = "$structure_after"' in run
    assert 'test "$roles_after" = "HARDENED_PHASE1"' in run
    assert "python -I scripts/fcb_db_roles.py login-check --phase 1 --require-tls" in run
    assert ('if [ "$roles_before" != "HARDENED_PHASE1" ] && { [ "$before" != "BRIDGE_0001_0008" ] '
            '|| [ "$roles_before" != "ROLES_ABSENT" ]; }; then') in run


def test_phase2_runs_its_files_once_with_the_observation_window_and_evidence_checks():
    run = _step(PHASE2)["run"]
    files = " ".join(f"-f sql/aiven/{f}" for f in TX2_FILES)
    assert (f'-v observe_seconds="$OBSERVE_SECONDS" -v expected_roles_state=HARDENED -1 -c "$GUARD" {files} >/dev/null'
            in _flat(run))
    assert 'test "$roles_after" = "HARDENED"' in run and 'test "$structure_before" = "$structure_after"' in run
    assert "python -I scripts/fcb_db_roles.py login-check --phase 2 --require-tls" in run
    assert "HARDENED_PHASE1|HARDENED) ;;" in run


def test_phase2_requires_authenticated_e2e_evidence_of_the_fcb_app_runtime():
    ev = _step(EVIDENCE)
    assert ev["env"]["E2E_RUN_ID"] == "${{ inputs.e2e_run_id }}" and ev["env"]["EXPECTED_SHA"] == "${{ github.sha }}"
    assert '[[ "$E2E_RUN_ID" =~ ^[0-9]{1,20}$ ]]' in ev["run"]
    assert 'gh api "repos/$REPO/actions/runs/$E2E_RUN_ID"' in ev["run"]
    assert "python3 -I -S scripts/fcb_db_roles.py e2e-evidence" in ev["run"]
    e2e = yaml.safe_load((ROOT / ".github" / "workflows" / "fcb-mcp-e2e.yml").read_text(encoding="utf-8"))
    assert e2e["run-name"].replace("${{ inputs.expected_db_identity || 'none' }}", "fcb_app") == _helper().E2E_TITLE


def test_the_administrator_reads_no_bridge_row_once_a_bridge_role_exists():
    """Review of 2add80b (P2): rows only AS fcb_app; the administrator runs catalog-only files,
    except the restore evidence of a first phase 1 (status quo: no bridge role yet)."""
    p1, p2, pre = _step(PHASE1)["run"], _step(PHASE2)["run"], _step(PRECHECK)["run"]
    assert "verify_bridge_schema.sql" not in p2 and "verify_bridge_schema.sql" not in pre
    first_only = p1.split("# First phase 1:", 1)[1].split("else", 1)[0]
    assert p1.count("verify_bridge_schema.sql") == 1 and "verify_bridge_schema.sql" in first_only
    for run in (p1, p2):
        assert run.count("python -I scripts/fcb_db_roles.py evidence") >= 1
        assert "count_bridge_catalog.sql" in run
    assert "default_transaction_read_only=on" not in p1 + p2  # read-only is not used as a boundary


def test_login_check_runs_isolated_with_hash_pinned_dependencies():
    for run in (_step(PHASE1)["run"], _step(PHASE2)["run"]):
        assert "PYTHONPATH" not in run
    pins = _step("Verify the pinned SHA-256")["run"]
    assert "pip install --quiet --require-hashes --no-deps -r scripts/fcb_db_roles_requirements.txt" in pins
    req = (ROOT / "scripts" / "fcb_db_roles_requirements.txt").read_text()
    for pkg in ("psycopg==3.3.6", "psycopg-binary==3.3.6", "typing-extensions=="):
        block = req.split(pkg, 1)[1].split("\n\n", 1)[0]
        assert re.search(r"--hash=sha256:[0-9a-f]{64}", block.split("\n", 2)[1]), pkg


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

def test_role_passwords_reach_only_their_phase_steps_and_never_a_command_line():
    for step in STEPS:
        env = step.get("env") or {}
        run = str(step.get("run", ""))
        for name in PASSWORD_SECRETS:
            if name in env:
                assert env[name] == f"${{{{ secrets.{name} }}}}"
                assert "inputs.mode == 'apply'" in step.get("if", "") or "inputs.mode == 'enable-migrator'" in step.get("if", ""), step["name"]
            assert f"${name}" not in run and f"${{{name}" not in run, step["name"]
        if "inputs.mode == 'apply'" in step.get("if", ""):
            assert "FCB_MIGRATOR_DB_PASSWORD" not in env, step["name"]  # phase 1 never sees it
    for prefix, var, target in ((PHASE1, "app_verifier", "FCB_APP_DB_VERIFIER"),
                                (PHASE2, "migrator_verifier", "FCB_MIGRATOR_DB_VERIFIER")):
        run = _step(prefix)["run"]
        assert f'{var}="$(python -I -S scripts/fcb_db_roles.py verifier ' in run
        assert not re.search(rf"(echo|printf)[^\n]*\${{?{var}", run), var
        assert f'{target}="${var}"' in run and "GITHUB_ENV" not in run


def test_psql_stderr_is_only_printed_after_redaction():
    for step in STEPS:
        run = str(step.get("run", ""))
        if '2>"$err"' in run:
            assert "scripts/fcb_db_roles.py sanitize \"$err\"" in run, step["name"]
            assert 'cat "$err"' not in run


def test_phase_sql_reads_verifiers_from_the_environment_and_embeds_no_credential():
    up = (AIVEN / "upsert_bridge_roles.sql").read_text()
    en = (AIVEN / "enable_bridge_migrator.sql").read_text()
    assert "\\getenv fcb_app_verifier FCB_APP_DB_VERIFIER" in up and "fcb_migrator_verifier" not in up
    assert "\\getenv fcb_migrator_verifier FCB_MIGRATOR_DB_VERIFIER" in en
    assert "ALTER ROLE fcb_app WITH LOGIN PASSWORD :'fcb_app_verifier';" in up
    assert "ALTER ROLE fcb_migrator WITH NOLOGIN PASSWORD NULL;" in up
    assert "ALTER ROLE fcb_signer WITH NOLOGIN PASSWORD NULL;" in up
    assert "ALTER ROLE fcb_migrator WITH LOGIN PASSWORD :'fcb_migrator_verifier';" in en
    for text in (up, en):
        assert not re.search(r"PASSWORD\s+'", text), "literal password"
    # Phase 1 grants no membership; phase 2 grants it to itself only inside the transaction.
    assert "GRANT fcb_migrator" not in _sql_only(up)
    code = _sql_only(en)
    assert code.index("GRANT fcb_migrator TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;") \
        < code.index("REVOKE fcb_migrator FROM CURRENT_USER;") < code.index("ALTER ROLE fcb_migrator WITH LOGIN")
    assert "FCB_ROLES_ADMIN_SESSION_PRESENT" in en and "pg_try_advisory_xact_lock" in en
    code = _sql_only(up)
    assert code.index("SET LOCAL createrole_self_grant = '';") < code.index("CREATE ROLE")
    for forbidden in ("SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS", "TO fcb_signer"):
        assert forbidden not in code.replace("NOLOGIN", ""), forbidden


def test_runtime_grants_are_identical_in_phase1_and_finalize():
    def grants(name):
        return [l.strip() for l in re.sub(r"\s+", " ", _sql_only((AIVEN / name).read_text())).split(";")
                if l.strip().startswith("GRANT ") and "fcb_app" in l and "SCHEMA" not in l]
    assert grants("grant_bridge_runtime.sql") == grants("finalize_bridge_privileges.sql") and len(grants("finalize_bridge_privileges.sql")) == 5


# ---------------------------------------------------------------- SQL hygiene

@pytest.mark.parametrize("name", NEW_SQL)
def test_role_sql_never_drops_or_changes_data(name):
    sql = re.sub(r"'[^']*'", "''", _sql_only((AIVEN / name).read_text())).upper()  # literals are data
    statements = [st.strip() for st in sql.split(";") if st.strip()]
    for st in statements:
        assert not re.match(r"(DROP|TRUNCATE|DELETE|UPDATE|INSERT|COPY|REASSIGN|CREATE (TABLE|FUNCTION|VIEW|TRIGGER))\b", st), (name, st[:60])
    for word in ("ALTER ROLE AVNADMIN", "SET ROLE", "SECURITY DEFINER", "DROP ROLE", "DROP OWNED"):
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


def test_phase1_needs_only_the_fcb_app_password_but_it_must_differ_from_any_migrator_one():
    helper = _helper()
    only_app = {k: v for k, v in GOOD.items() if k != "FCB_MIGRATOR_DB_PASSWORD"}
    assert helper.password_problem(only_app, roles=("fcb_app",)) is None
    assert helper.password_problem({**GOOD, "FCB_MIGRATOR_DB_PASSWORD": GOOD["FCB_APP_DB_PASSWORD"]},
                                   roles=("fcb_app",)) == "passwords_not_distinct"
    assert helper.MODE_ROLES == {"apply": ("fcb_app",), "enable-migrator": ("fcb_app", "fcb_migrator")}


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
    for mode in ("apply", "enable-migrator"):
        out = subprocess.run([sys.executable, "-I", "-S", str(HELPER), "check", "--mode", mode], env=env,
                             capture_output=True, text=True)
        assert out.returncode == 0 and GOOD["FCB_APP_DB_PASSWORD"] not in out.stdout + out.stderr
    out = subprocess.run([sys.executable, "-I", "-S", str(HELPER), "verifier", "FCB_APP_DB_PASSWORD"], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("SCRAM-SHA-256$4096:")
    assert GOOD["FCB_APP_DB_PASSWORD"] not in out.stdout
    weak = {**env, "FCB_APP_DB_PASSWORD": "weak"}
    for args in (["check", "--mode", "apply"], ["verifier", "FCB_APP_DB_PASSWORD"]):
        out = subprocess.run([sys.executable, "-I", "-S", str(HELPER), *args], env=weak, capture_output=True, text=True)
        assert out.returncode == 1 and out.stdout == "" and "weak" not in out.stderr
    for args in (["verifier", "FCB_DEFAULTDB_URL"], ["check"], ["check", "--mode", "x"], ["login-check"]):
        out = subprocess.run([sys.executable, "-I", "-S", str(HELPER), *args], env=env, capture_output=True, text=True)
        assert out.returncode == 2 and out.stdout == "", args


E2E_RUN = {"path": ".github/workflows/fcb-mcp-e2e.yml", "status": "completed", "conclusion": "success",
           "head_branch": "main", "event": "workflow_dispatch", "actor": {"login": "own"},
           "triggering_actor": {"login": "own"}, "head_sha": "a" * 40,
           "display_title": "FCB MCP E2E (expected database identity: fcb_app)", "created_at": "2026-11-10T10:00:00Z"}
E2E_ENV = {"REPO_OWNER": "own", "EXPECTED_SHA": "a" * 40, "NOW_EPOCH": "1794310800"}  # 2026-11-10T11:00:00Z


@pytest.mark.parametrize("change,why", [
    ({"path": ".github/workflows/other.yml"}, "not_the_mcp_e2e_workflow"),
    ({"conclusion": "failure"}, "not_successful"),
    ({"status": "in_progress"}, "not_successful"),
    ({"head_branch": "feature"}, "not_a_main_dispatch"),
    ({"event": "push"}, "not_a_main_dispatch"),
    ({"actor": {"login": "someone"}}, "not_owner_dispatched"),
    ({"triggering_actor": {"login": "someone"}}, "not_owner_dispatched"),
    ({"head_sha": "b" * 40}, "different_commit"),
    ({"display_title": "FCB MCP E2E (expected database identity: none)"}, "identity_not_asserted"),
    ({"created_at": "2026-11-09T10:00:00Z"}, "too_old_or_in_future"),
    ({"created_at": "2026-11-10T12:00:00Z"}, "too_old_or_in_future"),
    ({"created_at": "garbage"}, "no_timestamp"),
])
def test_e2e_evidence_refuses_anything_but_a_recent_owner_run_asserting_fcb_app(change, why):
    helper = _helper()
    assert helper.e2e_evidence_problem(E2E_RUN, E2E_ENV) is None
    assert helper.e2e_evidence_problem({**E2E_RUN, **change}, E2E_ENV) == why
    assert helper.e2e_evidence_problem(None, E2E_ENV) == "not_a_run"
    assert helper.e2e_evidence_problem(E2E_RUN, {**E2E_ENV, "EXPECTED_SHA": ""}) == "different_commit"


def test_e2e_evidence_cli(tmp_path):
    import json
    f = tmp_path / "run.json"
    f.write_text(json.dumps(E2E_RUN))
    env = {"PATH": "/usr/bin:/bin", **E2E_ENV}
    out = subprocess.run([sys.executable, "-I", "-S", str(HELPER), "e2e-evidence", str(f)], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0 and "asserted database identity fcb_app" in out.stdout
    f.write_text("not json")
    out = subprocess.run([sys.executable, "-I", "-S", str(HELPER), "e2e-evidence", str(f)], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 1 and "not_a_run" in out.stderr


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


# ---------------------------------------------------------------- fingerprints, counts and memberships

def test_structure_fingerprint_is_the_restore_fingerprint_without_owner_and_acl_fields():
    helper = _helper()
    full = (AIVEN / "fingerprint_bridge_schema.sql").read_text(encoding="utf-8")
    committed = (AIVEN / "fingerprint_bridge_structure.sql").read_text(encoding="utf-8")
    assert committed == helper.structure_fingerprint_sql(full), "regenerate: python scripts/fcb_db_roles.py structure-fingerprint-sql"
    body = committed.split("WITH s AS", 1)[1]
    for gone in ("c.relacl", "c.relowner", "p.proacl", "p.proowner"):
        assert gone not in body, gone
    assert "a.attacl" in body  # column privileges are still fingerprinted (must stay NULL)


def test_count_bridge_catalog_mirrors_the_privilege_independent_restore_counts():
    text = (AIVEN / "count_bridge_catalog.sql").read_text()
    for key in ("tables", "functions", "triggers", "indexes", "constraints_pg_constraint"):
        assert f"'{key}'::pg_catalog.text" in text
    assert "information_schema" not in _sql_only(text) and "public.schema_migrations" not in _sql_only(text)


def test_first_phase1_requires_the_restored_fingerprint():
    restore_env = yaml.safe_load(RESTORE_PATH.read_text(encoding="utf-8"))["jobs"]["restore"]["env"]
    assert JOB["env"]["EXPECTED_FINGERPRINT"] == restore_env["EXPECTED_FINGERPRINT"]
    assert 'test "$full_before" = "$EXPECTED_FINGERPRINT"' in _step(PHASE1)["run"]


def test_the_model_asserts_membership_reachability_and_phase():
    verify = (AIVEN / "verify_bridge_roles.sql").read_text()
    for code in ("membership_inventory", "escalation_path", "unexpected_app_or_signer_member", "unexpected_migrator_member",
                 "role_member_of_other_role", "session_reaches_migrator", "role_reachability", "owner_model",
                 "expression_functions", "routine_set", "trigger_set", "relation_hooks", "foreign_objects",
                 "role_settings"):
        assert f"'{code}'" in verify
    # Exactly the three automatic ADMIN-only grants (bootstrap superuser oid 10), no explicit one.
    assert verify.count("true, false, false, 10::pg_catalog.oid") == 3
    assert "'HARDENED_PHASE1'" in verify and "'HARDENED'" in verify
    diagnose = (AIVEN / "diagnose_bridge_roles.sql").read_text()
    for line in ("'membership '", "'effective '", "' grantor='", "'reach '", "'bridge_lock_holders '"):
        assert line in diagnose, line


def test_model_checks_are_parenthesized_so_a_false_and_mutation_disables_each_whole():
    verify = (AIVEN / "verify_bridge_roles.sql").read_text()
    checks = re.findall(r"SELECT '([a-z_]+)'(?:::pg_catalog\.text)?, (.)", verify)
    assert len(checks) >= 25 and all(first == "(" for _, first in checks)


def test_model_pins_the_same_routines_and_triggers_as_the_restore_precheck():
    pre = (AIVEN / "defaultdb_precheck.sql").read_text()
    verify = (AIVEN / "verify_bridge_roles.sql").read_text()
    for start, end in (("bridge_functions(sig) AS (VALUES", "\n)"), ("bridge_triggers(sig) AS (VALUES", "\n)")):
        block = pre[pre.index(start):pre.index(end, pre.index(start)) + 2]
        assert block in verify, start
    for code in ("routine_set", "trigger_set", "relation_hooks", "foreign_objects", "role_settings"):
        assert f"'{code}'" in verify


