"""FCB-WP-009 helper for the owner-gated roles workflow (fcb-aiven-defaultdb-roles.yml).

Nothing printed by this module contains a password, a SCRAM verifier, or the host, port,
user, password or database name of the administrative URI.

Subcommands (secrets are always read from the environment, never from argv):
  check --mode apply|enable-migrator
                         validate the role passwords the mode needs (stdlib, -I -S): apply needs
                         FCB_APP_DB_PASSWORD, enable-migrator both it and FCB_MIGRATOR_DB_PASSWORD
  verifier VAR           print the SCRAM-SHA-256 verifier of the password in $VAR (stdlib, -I -S);
                         the workflow keeps it in a shell variable and hands it to psql through
                         the environment (\\getenv); it is never echoed
  sanitize FILE          print one redacted line of a psql stderr capture (stdlib, -I -S)
  structure-fingerprint-sql
                         print sql/aiven/fingerprint_bridge_structure.sql, derived from
                         fingerprint_bridge_schema.sql (tests check the committed file equals it)
  e2e-evidence FILE      check a GitHub API run object (FILE) of fcb-mcp-e2e.yml: successful, on
                         main, owner-dispatched, same commit ($EXPECTED_SHA), recent, and named
                         for the expected database identity fcb_app (stdlib, -I -S)
  evidence               (python -I, psycopg) print the schema_migrations row count and rows, read
                         AS fcb_app: the administrator never reads a bridge table row
  bridge-state           (stdlib, -I -S) read `evidence` output on stdin; print the bridge state it is,
                         EXACTLY, among the admitted ones (8, 9 or 10: rows, names and checksums equal
                         to the repository's first N bridge migrations), else exit 1
  expected-migrations N  (stdlib, -I -S) print `version=name=sha256` of the first N bridge
                         migrations, space separated (the roles transaction's expected_migrations)
  login-check --phase 1|2 [--require-tls]
                         (python -I, psycopg from scripts/fcb_db_roles_requirements.txt) log in as
                         fcb_app (both phases), as fcb_migrator (phase 2; phase 1 proves its login
                         is refused) and, in phase 2, prove the administrator is denied on a bridge
                         table; every probe is rolled back
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

URL_VAR = "FCB_DEFAULTDB_URL"
PASSWORD_VARS = {"fcb_app": "FCB_APP_DB_PASSWORD", "fcb_migrator": "FCB_MIGRATOR_DB_PASSWORD"}
VERIFIER_VARS = ("FCB_APP_DB_VERIFIER", "FCB_MIGRATOR_DB_VERIFIER")
# URL-safe alphabet only: the owner builds a DSN from it without percent-encoding, and libpq
# and Python parse it identically (no '@', ':', '/', '%', '#', whitespace or quotes).
PASSWORD_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
SCRAM_ITERATIONS = 4096
VERIFIER_RE = re.compile(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/]+=*\$[A-Za-z0-9+/]+=*:[A-Za-z0-9+/]+=*")


MODE_ROLES = {"apply": ("fcb_app",), "enable-migrator": ("fcb_app", "fcb_migrator")}
# WP-009 compatibility (owner GO): the bridge states the roles workflow admits, exactly. 0009
# (#198, OAuth throttle) and 0010 (#199, certification ledger) are applied by their own
# owner-gated workflows as fcb_migrator, i.e. only after phase 2; the model then re-verifies.
BRIDGE_STATES = (8, 9, 10)
BRIDGE_DIR = Path(__file__).resolve().parents[1] / "sql" / "migrations" / "bridge"


def expected_migrations(state: int) -> list[str]:
    """`version=name=sha256` of the repository's bridge migrations 1..state (checksums of the files)."""
    rows = []
    for path in sorted(BRIDGE_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version = int(path.name[:4])
        if version <= state:
            rows.append(f"{version}={path.name}={hashlib.sha256(path.read_bytes()).hexdigest()}")
    if [int(r.split("=", 1)[0]) for r in rows] != list(range(1, state + 1)):
        raise ValueError("BRIDGE_SERIES_INCOMPLETE")
    return rows


def expected_evidence(state: int) -> str:
    rows = expected_migrations(state)
    return "\n".join([f"schema_migrations_rows={len(rows)}", *rows])


def bridge_state(evidence_text: str) -> int | None:
    """The admitted state whose rows the evidence equals exactly, else None (mixed, partial, extra
    or tampered rows: refused)."""
    text = evidence_text.strip()
    for state in BRIDGE_STATES:
        if text == expected_evidence(state):
            return state
    return None


def password_problem(env=None, roles=("fcb_app", "fcb_migrator")) -> str | None:
    """A non-sensitive category when a needed role password is unusable, else None."""
    env = os.environ if env is None else env
    values = {}
    for role in roles:
        var = PASSWORD_VARS[role]
        value = env.get(var, "")
        if not value:
            return f"{var}_missing"
        if not PASSWORD_RE.fullmatch(value):
            return f"{var}_not_32_to_128_url_safe_chars"
        values[role] = value
    other = [env.get(v, "") for r, v in PASSWORD_VARS.items() if r not in roles]
    if len(set(values.values())) != len(values) or any(o and o in values.values() for o in other):
        return "passwords_not_distinct"
    try:
        admin = unquote(urlsplit(env.get(URL_VAR, "")).password or "")
    except ValueError:
        admin = ""
    if admin and admin in values.values():
        return "password_equals_administrative_password"
    return None


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS) -> str:
    """PostgreSQL's SCRAM-SHA-256 verifier (RFC 5802/7677), as pg_authid stores it.

    The password alphabet is ASCII-only (PASSWORD_RE), for which SASLprep is the identity.
    """
    salt = secrets.token_bytes(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


_STRUCTURE_REPLACEMENTS = (
    ("' acl='::pg_catalog.text\n        OPERATOR(pg_catalog.||) COALESCE(c.relacl::pg_catalog.text, '-'::pg_catalog.text)",
     "' acl=*'::pg_catalog.text"),
    ("' own='::pg_catalog.text\n        OPERATOR(pg_catalog.||) (c.relowner OPERATOR(pg_catalog.=) (SELECT me.oid FROM me))::pg_catalog.text",
     "' own=*'::pg_catalog.text"),
    ("' acl='::pg_catalog.text OPERATOR(pg_catalog.||) COALESCE(p.proacl::pg_catalog.text, '-'::pg_catalog.text)",
     "' acl=*'::pg_catalog.text"),
    ("' own='::pg_catalog.text\n        OPERATOR(pg_catalog.||) (p.proowner OPERATOR(pg_catalog.=) (SELECT me.oid FROM me))::pg_catalog.text",
     "' own=*'::pg_catalog.text"),
)
STRUCTURE_HEADER = (
    "-- FCB-WP-009: GENERATED by `python scripts/fcb_db_roles.py structure-fingerprint-sql` from\n"
    "-- fingerprint_bridge_schema.sql; do not edit. Identical catalog fingerprint EXCEPT the relation\n"
    "-- and routine owner (own=) and ACL (acl=) fields, which the WP-009 roles apply changes BY DESIGN\n"
    "-- (ownership to fcb_migrator, runtime allowlist). Column ACLs, columns, defaults, constraints,\n"
    "-- indexes, triggers, rules, policies, functions' bodies and types are all still covered: equal\n"
    "-- before and after the apply proves the bridge structure is unchanged. Read-only.\n"
)


def structure_fingerprint_sql(full: str) -> str:
    out = full
    for old, new in _STRUCTURE_REPLACEMENTS:
        if out.count(old) != 1:
            raise ValueError("fingerprint_bridge_schema.sql changed; update the structure transform")
        out = out.replace(old, new)
    return STRUCTURE_HEADER + out


def _diag():
    """scripts/aiven_pg18_diag.py, loaded by path (python -I -S does not add the script dir)."""
    path = Path(__file__).resolve().with_name("aiven_pg18_diag.py")
    spec = importlib.util.spec_from_file_location("fcb_aiven_pg18_diag", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sanitize(text: str, env=None) -> str:
    """One redacted line: URI parts (as the restore workflow), role passwords and verifiers,
    and every line that could quote a statement carrying a verifier are removed."""
    env = os.environ if env is None else env
    kept = [line for line in text.splitlines()
            if not re.search(r"SCRAM-SHA-256|PASSWORD|^\s*LINE \d+:|^\s*\^", line, re.I)]
    out = "\n".join(kept)
    raw = env.get(URL_VAR, "")
    if raw:
        out = _diag().sanitize(out, raw)
    for var in (*PASSWORD_VARS.values(), *VERIFIER_VARS):
        value = env.get(var, "")
        if len(value) >= 4:
            out = out.replace(value, "<redacted>")
    out = " | ".join(part.strip() for part in out.splitlines() if part.strip())
    return out[:300]


def _connect_as(role: str, env):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    params = conninfo_to_dict(env[URL_VAR])
    params["user"] = role
    params["password"] = env[PASSWORD_VARS[role]]
    params.setdefault("connect_timeout", "15")
    return psycopg.connect(**params)


def _denied(conn, statement: str) -> bool:
    """True when `statement` is refused for lack of privilege. Always rolled back."""
    import psycopg

    try:
        with conn.transaction():
            conn.execute(statement)
            # Never commit a probe, also when it unexpectedly succeeds (e.g. the migrator's CREATE).
            raise psycopg.Rollback()
    except psycopg.errors.InsufficientPrivilege:
        return True
    return False


def _repo_on_path() -> None:
    # Runs under `python -I` (no PYTHONPATH, no user site, no script dir on sys.path): the
    # repository root is added explicitly, nothing else.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)


def _check_role(role: str, env, require_tls: bool) -> str:
    """Read-only checks for one identity; every probe runs in a transaction rolled back."""
    from psycopg.rows import dict_row

    with _connect_as(role, env) as conn:
        conn.autocommit = True
        row = conn.execute(
            "SELECT CURRENT_USER::pg_catalog.text AS who, COALESCE((SELECT s.ssl FROM pg_catalog.pg_stat_ssl s "
            "WHERE s.pid OPERATOR(pg_catalog.=) pg_catalog.pg_backend_pid()), false) AS tls").fetchone()
        if row[0] != role:
            raise RuntimeError("FCB_LOGIN_IDENTITY_MISMATCH")
        if require_tls and row[1] is not True:
            raise RuntimeError("FCB_LOGIN_NOT_TLS")
        migrations = conn.execute("SELECT pg_catalog.count(*) FROM public.schema_migrations").fetchone()[0]
        create_denied = _denied(conn, "CREATE TABLE public.fcb_wp009_login_probe (i integer)")
        if role == "fcb_app":
            _repo_on_path()
            from bridge import schema_readiness

            def app_connect():
                c = _connect_as("fcb_app", env)
                c.row_factory = dict_row
                return c

            readiness = schema_readiness.evaluate(app_connect)["state"]
            legacy_denied = _denied(conn, "SELECT 1 FROM public.model_snapshots LIMIT 0")
            # WP-009 compatibility: one of the admitted bridge states (the exact rows, checksums,
            # catalog and structure of that state are proved by the workflow before this check)
            ok = readiness == "READY" and create_denied and legacy_denied and migrations in BRIDGE_STATES
            line = (f"login {role}: tls={row[1]} schema_migrations_rows={migrations} readiness={readiness} "
                    f"create_denied={create_denied} legacy_read_denied={legacy_denied}")
        else:
            # The migrator owns the bridge: CREATE succeeds, but inside a rolled-back transaction.
            ok = migrations in BRIDGE_STATES and not create_denied
            line = f"login {role}: tls={row[1]} schema_migrations_rows={migrations} can_create={not create_denied}"
    if not ok:
        raise RuntimeError("FCB_LOGIN_MODEL_MISMATCH " + line)
    return line


def _check_migrator_login_refused(env) -> str:
    """Phase 1: fcb_migrator is NOLOGIN without password; any login attempt must fail."""
    import psycopg

    probe = dict(env)
    probe[PASSWORD_VARS["fcb_migrator"]] = probe.get(PASSWORD_VARS["fcb_migrator"]) or secrets.token_urlsafe(32)
    try:
        _connect_as("fcb_migrator", probe).close()
    except psycopg.OperationalError:
        return "login fcb_migrator: refused (phase 1: NOLOGIN, no password)"
    raise RuntimeError("FCB_LOGIN_MIGRATOR_NOT_REFUSED")


def _check_admin_denied(env) -> str:
    """Phase 2: the administrator holds no privilege on the bridge. The probe is a privilege
    check only: PostgreSQL refuses the SELECT before reading a row or evaluating any policy."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    with psycopg.connect(**{"connect_timeout": "15", **conninfo_to_dict(env[URL_VAR])}) as conn:
        conn.autocommit = True
        reach = conn.execute("SELECT pg_catalog.pg_has_role(CURRENT_USER, 'fcb_migrator', 'USAGE'), "
                             "pg_catalog.pg_has_role(CURRENT_USER, 'fcb_migrator', 'SET')").fetchone()
        denied = _denied(conn, "SELECT 1 FROM public.schema_migrations LIMIT 0")
    if reach != (False, False) or not denied:
        raise RuntimeError("FCB_LOGIN_ADMIN_NOT_DENIED")
    return "admin: bridge access denied=True inherits_migrator=False can_set_migrator=False"


def login_check(require_tls: bool, phase: int = 2, env=None) -> int:
    env = os.environ if env is None else env
    rc = 0
    checks = [("fcb_app", lambda: _check_role("fcb_app", env, require_tls))]
    if phase == 1:
        checks.append(("fcb_migrator", lambda: _check_migrator_login_refused(env)))
    else:
        checks.append(("fcb_migrator", lambda: _check_role("fcb_migrator", env, require_tls)))
        checks.append(("admin", lambda: _check_admin_denied(env)))
    for name, check in checks:
        try:
            print(check())
        except Exception as exc:  # noqa: BLE001 - only the class and SQLSTATE are printed
            code = getattr(exc, "sqlstate", None) or "-"
            reason = str(exc) if str(exc).startswith("FCB_") else type(exc).__name__
            print(f"::error::login {name}: failed ({reason}, sqlstate={code})", file=sys.stderr)
            rc = 1
    return rc


def schema_evidence(env=None) -> list[str]:
    """The only bridge ROWS the workflow needs, read AS fcb_app (never as the administrator):
    `schema_migrations_rows=N` then one `version=name=sha256` line per migration. Object counts
    come from sql/aiven/count_bridge_catalog.sql (catalog only)."""
    env = os.environ if env is None else env
    with _connect_as("fcb_app", env) as conn:
        conn.autocommit = True
        rows = conn.execute("SELECT m.version, m.name, m.sha256 FROM public.schema_migrations m "
                            "ORDER BY m.version").fetchall()
    return [f"schema_migrations_rows={len(rows)}", *("=".join(str(v) for v in r) for r in rows)]


E2E_WORKFLOW_PATH = ".github/workflows/fcb-mcp-e2e.yml"
E2E_TITLE = "FCB MCP E2E (expected database identity: fcb_app)"


def e2e_evidence_problem(run: dict, env=None) -> str | None:
    """A non-sensitive category when the E2E run does not prove the fcb_app runtime, else None."""
    import datetime as _dt

    env = os.environ if env is None else env
    owner = env.get("REPO_OWNER", "")
    expected_sha = env.get("EXPECTED_SHA", "")
    max_age = float(env.get("E2E_MAX_AGE_HOURS", "6"))
    if not isinstance(run, dict):
        return "not_a_run"
    if run.get("path") != E2E_WORKFLOW_PATH:
        return "not_the_mcp_e2e_workflow"
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return "not_successful"
    if run.get("head_branch") != "main" or run.get("event") != "workflow_dispatch":
        return "not_a_main_dispatch"
    if not owner or (run.get("actor") or {}).get("login") != owner \
            or (run.get("triggering_actor") or {}).get("login") != owner:
        return "not_owner_dispatched"
    if not expected_sha or run.get("head_sha") != expected_sha:
        return "different_commit"
    if run.get("display_title") != E2E_TITLE:
        return "identity_not_asserted"
    try:
        created = _dt.datetime.fromisoformat(str(run.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        return "no_timestamp"
    now = _dt.datetime.fromtimestamp(float(env["NOW_EPOCH"]), _dt.timezone.utc) if env.get("NOW_EPOCH") \
        else _dt.datetime.now(_dt.timezone.utc)
    age = (now - created).total_seconds() / 3600
    if age < 0 or age > max_age:
        return "too_old_or_in_future"
    return None


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "check" and len(argv) == 4 and argv[2] == "--mode" and argv[3] in MODE_ROLES:
        why = password_problem(roles=MODE_ROLES[argv[3]])
        if why:
            print(f"::error::role passwords refused ({why})", file=sys.stderr)
            return 1
        print(f"role passwords for {argv[3]}: present, distinct, 32-128 URL-safe characters")
        return 0
    if cmd == "verifier" and len(argv) == 3 and argv[2] in PASSWORD_VARS.values():
        role = next(r for r, v in PASSWORD_VARS.items() if v == argv[2])
        if password_problem(roles=(role,)):
            print("::error::role password refused; run `check` first", file=sys.stderr)
            return 1
        print(scram_verifier(os.environ[argv[2]]))
        return 0
    if cmd == "e2e-evidence" and len(argv) == 3:
        import json as _json
        try:
            with open(argv[2], encoding="utf-8") as fh:
                run = _json.load(fh)
        except (OSError, ValueError):
            run = None
        why = e2e_evidence_problem(run)
        if why:
            print(f"::error::MCP E2E evidence refused ({why})", file=sys.stderr)
            return 1
        print("MCP E2E evidence: successful owner run on this main commit asserted database identity fcb_app")
        return 0
    if cmd == "evidence" and len(argv) == 2:
        for line in schema_evidence():
            print(line)
        return 0
    if cmd == "sanitize" and len(argv) == 3:
        try:
            with open(argv[2], encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
        print(sanitize(text) or "<empty psql message>")
        return 0
    if cmd == "structure-fingerprint-sql" and len(argv) == 2:
        full = Path(__file__).resolve().parents[1] / "sql" / "aiven" / "fingerprint_bridge_schema.sql"
        sys.stdout.write(structure_fingerprint_sql(full.read_text(encoding="utf-8")))
        return 0
    if cmd == "bridge-state" and len(argv) == 2:
        state = bridge_state(sys.stdin.read())
        if state is None:
            print("::error::schema_migrations rows are not exactly bridge 0001-0008, 0001-0009 or 0001-0010",
                  file=sys.stderr)
            return 1
        print(state)
        return 0
    if cmd == "expected-migrations" and len(argv) == 3 and argv[2] in tuple(str(s) for s in BRIDGE_STATES):
        print(" ".join(expected_migrations(int(argv[2]))))
        return 0
    if cmd == "login-check" and len(argv) >= 4 and argv[2] == "--phase" and argv[3] in ("1", "2") \
            and argv[4:] in ([], ["--require-tls"]):
        return login_check(require_tls=argv[4:] == ["--require-tls"], phase=int(argv[3]))
    print("::error::fcb_db_roles: unknown subcommand", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
